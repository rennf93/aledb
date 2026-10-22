"""
Correct flask numbers from a CSV, with a dry run, a collision check, an audit
trail in the database (Flask.comments and AleExperiment.notes) and an audit CSV.

The displayed sample name "A<ale> F<flask> I<isolate> R<rep>" is built live from
AleId.ale_id, Flask.flask_number, Isolate.isolate_number and
TechnicalReplicate.tech_rep_number, so changing Flask.flask_number renames every
sample in that flask everywhere on the site. Nothing else is touched: breseq
report paths (ResequencingExperiment.location) keep the folder names they were
uploaded with, and the mutation data is untouched.

Input CSV columns (header required, extra columns ignored):
    experiment_id, ale_number, old_flask_number, new_flask_number

Checks before anything is written: every experiment, ALE and old flask exists;
old != new; no two rows target the same flask; after the change every ALE still
has unique flask numbers. Renumbering is done in two phases inside one
transaction (old -> negative placeholder -> new) so that swaps and chains never
hit the (ale, flask_number) unique constraint part-way.

Audit trail written on --execute:
    Flask.comments        "<date>: flask number corrected <old> -> <new> (<source>)"
    AleExperiment.notes   one dated line per experiment listing every change
    --audit-csv           flask_pk, experiment_id, ale_number, old, new, samples

To undo, run the command again with the audit CSV's old/new columns swapped.

Examples:
    manage.py renumber_flasks changes.csv --source "colleague's numbering sheet 2026-10-16"
    manage.py renumber_flasks changes.csv --source "..." --execute --audit-csv /tmp/audit.csv
"""
import csv
import datetime
from collections import defaultdict

from django.core.management import BaseCommand, CommandError
from django.db import transaction

from ale.models import AleExperiment, AleId, Flask
from seq.models import ResequencingExperiment


class DryRunRollback(Exception):
    pass


def _sample_labels(flask):
    reseq = ResequencingExperiment.objects.filter(tech_rep__isolate__flask=flask)
    return sorted(r.ale_flask_isolate_str for r in reseq)


def _experiment_labels(experiment):
    reseq = ResequencingExperiment.objects.filter(
        tech_rep__isolate__flask__ale_id__ale_experiment=experiment)
    return sorted(r.ale_flask_isolate_str for r in reseq)


def read_changes(path):
    required = ('experiment_id', 'ale_number', 'old_flask_number', 'new_flask_number')
    changes = []
    with open(path, newline='') as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise CommandError("CSV is missing columns: %s" % ', '.join(missing))
        for line_no, row in enumerate(reader, start=2):
            try:
                changes.append(tuple(int(row[c]) for c in required))
            except ValueError:
                raise CommandError("line %d: non-integer value in %r" % (line_no, row))
    if not changes:
        raise CommandError("CSV holds no changes")
    return changes


def validate(changes):
    """Resolve every change to a Flask row and check the result is collision-free."""
    resolved = []        # (flask, new_number)
    seen_targets = set()
    per_ale_old = defaultdict(set)
    per_ale_new = defaultdict(set)
    for experiment_id, ale_number, old, new in changes:
        if old == new:
            raise CommandError("experiment %d A%d: old and new flask number are both %d"
                               % (experiment_id, ale_number, old))
        if new < 0:
            raise CommandError("negative flask numbers are reserved as placeholders")
        try:
            experiment = AleExperiment.objects.get(pk=experiment_id)
        except AleExperiment.DoesNotExist:
            raise CommandError("no experiment %d" % experiment_id)
        try:
            ale = AleId.objects.get(ale_experiment=experiment, ale_id=ale_number)
        except AleId.DoesNotExist:
            raise CommandError("experiment %d has no ALE A%d" % (experiment_id, ale_number))
        try:
            flask = Flask.objects.get(ale_id=ale, flask_number=old)
        except Flask.DoesNotExist:
            raise CommandError("experiment %d A%d has no flask F%d" % (experiment_id, ale_number, old))
        if flask.pk in seen_targets:
            raise CommandError("experiment %d A%d F%d is listed twice" % (experiment_id, ale_number, old))
        seen_targets.add(flask.pk)
        per_ale_old[ale.pk].add(old)
        per_ale_new[ale.pk].add(new)
        resolved.append((flask, new))

    for ale_pk, news in per_ale_new.items():
        existing = set(Flask.objects.filter(ale_id_id=ale_pk).values_list('flask_number', flat=True))
        kept = existing - per_ale_old[ale_pk]
        clash = kept & news
        if clash:
            ale = AleId.objects.get(pk=ale_pk)
            raise CommandError("experiment %d A%d: new flask numbers %s collide with flasks that keep "
                               "their number" % (ale.ale_experiment_id, ale.ale_id, sorted(clash)))
        if len(news) != len([n for f, n in resolved if f.ale_id_id == ale_pk]):
            ale = AleId.objects.get(pk=ale_pk)
            raise CommandError("experiment %d A%d: two flasks are given the same new number"
                               % (ale.ale_experiment_id, ale.ale_id))
    return resolved


def apply_changes(resolved, source, today, out):
    # Phase 1: park every renamed flask on a negative placeholder (unique: -(pk)).
    for flask, new in resolved:
        Flask.objects.filter(pk=flask.pk).update(flask_number=-flask.pk)
    # Phase 2: final numbers plus the per-flask audit comment.
    note_lines = defaultdict(list)
    for flask, new in resolved:
        comment = "%s: flask number corrected %d -> %d (%s)" % (today, flask.flask_number, new, source)
        if flask.comments:
            comment = flask.comments + "; " + comment
        Flask.objects.filter(pk=flask.pk).update(flask_number=new, comments=comment[:200])
        ale = flask.ale_id
        note_lines[ale.ale_experiment_id].append("A%d F%d->F%d" % (ale.ale_id, flask.flask_number, new))
    # Per-experiment note.
    for experiment_id, items in note_lines.items():
        experiment = AleExperiment.objects.get(pk=experiment_id)
        line = "%s: flask numbers corrected per %s: %s (%d flasks). Breseq report folders keep the " \
               "upload numbering." % (today, source, ', '.join(items), len(items))
        notes = (experiment.notes + "\n" + line) if experiment.notes else line
        AleExperiment.objects.filter(pk=experiment_id).update(notes=notes)
        out.write("  note added to experiment %d" % experiment_id)


class Command(BaseCommand):
    help = "Correct flask numbers from a CSV (dry run unless --execute)."

    def add_arguments(self, parser):
        parser.add_argument('csv_path', help="CSV with experiment_id, ale_number, old_flask_number, new_flask_number")
        parser.add_argument('--source', required=True,
                            help="where the correct numbers came from; recorded in flask comments and experiment notes")
        parser.add_argument('--execute', action='store_true', help="commit; without it everything is rolled back")
        parser.add_argument('--audit-csv', help="write flask_pk/old/new/samples for every change to this path")
        parser.add_argument('--date', default=datetime.date.today().isoformat(),
                            help="date stamp for the notes (default today)")

    def handle(self, *args, **options):
        changes = read_changes(options['csv_path'])
        resolved = validate(changes)
        execute = options['execute']
        self.stdout.write("%s: %d flask number changes in %d experiment(s)" % (
            'EXECUTE' if execute else 'DRY RUN', len(resolved),
            len({f.ale_id.ale_experiment_id for f, n in resolved})))

        # Plan, with the sample names each change renames, and the expected end state.
        expected = {}
        audit_rows = []
        for flask, new in resolved:
            ale = flask.ale_id
            before = _sample_labels(flask)
            after = [s.replace(" F%d " % flask.flask_number, " F%d " % new, 1) for s in before]
            self.stdout.write("  exp %d A%d F%d -> F%d  (%d samples: %s)" % (
                ale.ale_experiment_id, ale.ale_id, flask.flask_number, new, len(before), ', '.join(before)))
            audit_rows.append([flask.pk, ale.ale_experiment_id, ale.ale_id, flask.flask_number, new,
                               '; '.join(before)])
            expected.setdefault(ale.ale_experiment_id, {})
            expected[ale.ale_experiment_id].update(dict(zip(before, after)))

        try:
            with transaction.atomic():
                before_labels = {eid: _experiment_labels(AleExperiment.objects.get(pk=eid)) for eid in expected}
                apply_changes(resolved, options['source'], options['date'], self.stdout)
                self.stdout.write("Verifying:")
                for eid, renames in expected.items():
                    want = sorted(renames.get(s, s) for s in before_labels[eid])
                    got = _experiment_labels(AleExperiment.objects.get(pk=eid))
                    if want != got:
                        raise CommandError("experiment %d: sample names after change do not match the plan; "
                                           "rolled back. missing=%s unexpected=%s" % (
                                               eid, sorted(set(want) - set(got)), sorted(set(got) - set(want))))
                    self.stdout.write("  OK experiment %d: %d samples, %d renamed, %d unchanged" % (
                        eid, len(got), len(renames), len(got) - len(renames)))
                if not execute:
                    raise DryRunRollback()
        except DryRunRollback:
            self.stdout.write("Dry run complete. Rolled back; nothing was written. Re-run with --execute to commit.")
            return

        if options['audit_csv']:
            with open(options['audit_csv'], 'w', newline='') as fh:
                writer = csv.writer(fh)
                writer.writerow(['flask_pk', 'experiment_id', 'ale_number', 'old_flask_number',
                                 'new_flask_number', 'samples_before'])
                writer.writerows(audit_rows)
            self.stdout.write("Audit CSV written to %s" % options['audit_csv'])
        self.stdout.write("Committed.")
