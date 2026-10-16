"""
Deep-copy a project: a new Project row plus copies of every experiment and all
rows that hang off them, so the copy can be curated (or broken) without touching
the original.

What is copied, per experiment, with every foreign key remapped to the new rows:
    AleExperiment -> AleId -> Flask -> Isolate -> TechnicalReplicate
      -> ResequencingExperiment -> ObservedMutation, UnassignedMissingCoverageEvidence
    EnrichmentMutation, FixatedMutation, ConvergeMutation, AleExperimentFilter,
    bibliome.Publication
    guardian object permissions on the project (same users/groups, same codenames)
    stats.StaticData (the Stats page cache, keyed by experiment id with no foreign key)
      is not copied but regenerated for each new experiment from the copied rows, so it
      reflects the current filters rather than the state at the original upload.

What is shared, not copied: the Mutation catalog, Media, FreezerBox, Instrument,
User. ResequencingExperiment.location (breseq report path on disk) is copied as
text, so the copy's report links point at the original files.

The copy is always private (is_public=False) regardless of the source.
Experiment `date` values are preserved; the new Project's date is now.

Default is a dry run: every insert is performed inside a transaction, the
source/copy row counts are compared, then the transaction is rolled back.
Pass --execute to commit.

Examples:
    manage.py clone_project 24 IND_2026
    manage.py clone_project 24 IND_2026 --execute
    manage.py clone_project 24 IND_2026 --owner someuser --description "curation sandbox"
"""
import copy

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.management import BaseCommand, CommandError
from django.db import transaction
from guardian.models import GroupObjectPermission, UserObjectPermission
from guardian.shortcuts import assign_perm

from ale.models import (AleExperiment, AleId, Flask, Isolate, Project,
                        TechnicalReplicate)
from bibliome.models import Publication
from converge.models import ConvergeMutation
from enrichment.models import EnrichmentMutation
from filter.models import AleExperimentFilter
from fixation.models import FixatedMutation
from seq.models import (ObservedMutation, ResequencingExperiment,
                        UnassignedMissingCoverageEvidence)
from stats.models import StaticData
from stats.util import generate_static_data

BATCH = 2000

# Tables that hang directly off AleExperiment and have no children of their own.
PER_EXPERIMENT_LEAF_MODELS = (EnrichmentMutation, FixatedMutation, ConvergeMutation,
                              AleExperimentFilter, Publication)


class DryRunRollback(Exception):
    pass


def _copy(obj, **overrides):
    """Shallow copy of a model instance with pk cleared, ready to be saved as a new row."""
    new = copy.copy(obj)
    new.pk = None
    new._state.adding = True
    for name, value in overrides.items():
        setattr(new, name, value)
    return new


def _bulk_copy(queryset, **overrides):
    rows = [_copy(obj, **overrides) for obj in queryset.iterator()]
    model = queryset.model
    for start in range(0, len(rows), BATCH):
        model.objects.bulk_create(rows[start:start + BATCH])
    return len(rows)


def _experiment_counts(experiment):
    reseq = ResequencingExperiment.objects.filter(
        tech_rep__isolate__flask__ale_id__ale_experiment=experiment)
    counts = {
        'ales': AleId.objects.filter(ale_experiment=experiment).count(),
        'flasks': Flask.objects.filter(ale_id__ale_experiment=experiment).count(),
        'isolates': Isolate.objects.filter(flask__ale_id__ale_experiment=experiment).count(),
        'tech_reps': TechnicalReplicate.objects.filter(
            isolate__flask__ale_id__ale_experiment=experiment).count(),
        'reseq': reseq.count(),
        'observed_mutations': ObservedMutation.objects.filter(
            sequencing_experiment__in=reseq).count(),
        'missing_coverage': UnassignedMissingCoverageEvidence.objects.filter(
            sequencing_experiment__in=reseq).count(),
    }
    for model in PER_EXPERIMENT_LEAF_MODELS:
        counts[model.__name__] = model.objects.filter(ale_experiment=experiment).count()
    counts['sample_names'] = sorted(r.ale_flask_isolate_str for r in reseq)
    return counts


def _copy_permissions(source, target):
    content_type = ContentType.objects.get_for_model(Project)
    copied = 0
    for perm in UserObjectPermission.objects.filter(content_type=content_type,
                                                    object_pk=str(source.pk)):
        assign_perm(perm.permission.codename, perm.user, target)
        copied += 1
    for perm in GroupObjectPermission.objects.filter(content_type=content_type,
                                                     object_pk=str(source.pk)):
        assign_perm(perm.permission.codename, perm.group, target)
        copied += 1
    return copied


def clone_experiment(experiment, new_project, isolate_map):
    """Copy one experiment and everything under it into new_project.

    isolate_map (old isolate id -> new Isolate) is shared across the project so
    that parent_isolate / starting_strain links can be remapped afterwards.
    """
    new_experiment = _copy(experiment, project=new_project)
    new_experiment.save()
    # auto_now_add overwrote the date on save; put the original back.
    AleExperiment.objects.filter(pk=new_experiment.pk).update(date=experiment.date)

    ale_map = {}
    for ale in AleId.objects.filter(ale_experiment=experiment).order_by('pk'):
        new_ale = _copy(ale, ale_experiment=new_experiment)
        new_ale.save()
        ale_map[ale.pk] = new_ale

    flask_map = {}
    for flask in Flask.objects.filter(ale_id__ale_experiment=experiment).order_by('pk'):
        new_flask = _copy(flask, ale_id=ale_map[flask.ale_id_id])
        new_flask.save()
        flask_map[flask.pk] = new_flask

    for isolate in Isolate.objects.filter(flask__ale_id__ale_experiment=experiment).order_by('pk'):
        new_isolate = _copy(isolate, flask=flask_map[isolate.flask_id])
        new_isolate.save()
        isolate_map[isolate.pk] = new_isolate

    tech_rep_map = {}
    for tech_rep in TechnicalReplicate.objects.filter(
            isolate__flask__ale_id__ale_experiment=experiment).order_by('pk'):
        new_tech_rep = _copy(tech_rep, isolate=isolate_map[tech_rep.isolate_id])
        new_tech_rep.save()
        tech_rep_map[tech_rep.pk] = new_tech_rep

    for reseq in ResequencingExperiment.objects.filter(
            tech_rep__isolate__flask__ale_id__ale_experiment=experiment).order_by('pk'):
        new_reseq = _copy(reseq, tech_rep=tech_rep_map[reseq.tech_rep_id])
        new_reseq.save()
        _bulk_copy(ObservedMutation.objects.filter(sequencing_experiment=reseq),
                   sequencing_experiment=new_reseq)
        _bulk_copy(UnassignedMissingCoverageEvidence.objects.filter(sequencing_experiment=reseq),
                   sequencing_experiment=new_reseq)

    for model in PER_EXPERIMENT_LEAF_MODELS:
        _bulk_copy(model.objects.filter(ale_experiment=experiment), ale_experiment=new_experiment)

    # Stats page cache: primary key == experiment id, no FK, so the Stats page 500s without it.
    generate_static_data(new_experiment.pk)

    return new_experiment, ale_map


def clone_project(source, new_name, owner, description, out):
    new_project = _copy(source, name=new_name, user=owner, is_public=False,
                        description=description)
    new_project.save()
    out.write("Created project %d '%s' (owner %s, private)" % (new_project.pk, new_project.name,
                                                               owner.username))
    out.write("Copied %d object permissions" % _copy_permissions(source, new_project))

    isolate_map = {}
    pairs = []
    ale_maps = {}
    for experiment in source.aleexperiment_set.order_by('pk'):
        new_experiment, ale_map = clone_experiment(experiment, new_project, isolate_map)
        pairs.append((experiment, new_experiment))
        ale_maps.update(ale_map)
        out.write("  experiment %d '%s' -> %d" % (experiment.pk, experiment.name, new_experiment.pk))

    # Second pass: links between isolates (and from ALEs to isolates) that may cross
    # experiments inside the project. Links to isolates outside the project are kept as-is.
    remapped = 0
    for old_id, new_isolate in isolate_map.items():
        old_parent = Isolate.objects.get(pk=old_id).parent_isolate_id
        if old_parent in isolate_map:
            new_isolate.parent_isolate = isolate_map[old_parent]
            new_isolate.save(update_fields=['parent_isolate'])
            remapped += 1
    for old_id, new_ale in ale_maps.items():
        old_strain = AleId.objects.get(pk=old_id).starting_strain_id
        if old_strain in isolate_map:
            new_ale.starting_strain = isolate_map[old_strain]
            new_ale.save(update_fields=['starting_strain'])
            remapped += 1
    out.write("Remapped %d intra-project isolate links" % remapped)
    return new_project, pairs


def verify(pairs, out):
    """Compare source and copy counts (and sample names) for every experiment pair."""
    ok = True
    for source, target in pairs:
        src, dst = _experiment_counts(source), _experiment_counts(target)
        same = src == dst
        ok = ok and same
        flat = ', '.join('%s=%s' % (k, v) for k, v in src.items() if k != 'sample_names')
        has_stats = StaticData.objects.filter(id=target.pk).exists()
        ok = ok and has_stats
        out.write("  %s %d -> %d: %s; %d sample names %s; stats cache %s" % (
            'OK  ' if same and has_stats else 'DIFF', source.pk, target.pk, flat,
            len(src['sample_names']),
            'identical' if src['sample_names'] == dst['sample_names'] else 'DIFFER',
            'built' if has_stats else 'MISSING'))
        if not same:
            for key in src:
                if src[key] != dst[key] and key != 'sample_names':
                    out.write("        %s: source=%s copy=%s" % (key, src[key], dst[key]))
    return ok


class Command(BaseCommand):
    help = "Deep-copy a project and all of its experiments into a new, private project."

    def add_arguments(self, parser):
        parser.add_argument('project_id', type=int, help="id of the project to copy")
        parser.add_argument('new_name', help="name of the new project (max 50 chars, must be unused)")
        parser.add_argument('--execute', action='store_true',
                            help="commit the copy; without this flag everything is rolled back")
        parser.add_argument('--owner', help="username to own the copy (default: source owner)")
        parser.add_argument('--description', help="description for the copy (default: source's)")

    def handle(self, *args, **options):
        try:
            source = Project.objects.get(pk=options['project_id'])
        except Project.DoesNotExist:
            raise CommandError("no project with id %s" % options['project_id'])
        new_name = options['new_name']
        if len(new_name) > Project._meta.get_field('name').max_length:
            raise CommandError("new name is longer than %d characters"
                               % Project._meta.get_field('name').max_length)
        if Project.objects.filter(name=new_name).exists():
            raise CommandError("a project named '%s' already exists" % new_name)
        owner = source.user
        if options['owner']:
            try:
                owner = User.objects.get(username=options['owner'])
            except User.DoesNotExist:
                raise CommandError("no user '%s'" % options['owner'])
        description = options['description']
        if description is None:
            description = source.description

        execute = options['execute']
        self.stdout.write("%s: copying project %d '%s' (%d experiments) to '%s'" % (
            'EXECUTE' if execute else 'DRY RUN', source.pk, source.name,
            source.aleexperiment_set.count(), new_name))
        try:
            with transaction.atomic():
                new_project, pairs = clone_project(source, new_name, owner, description, self.stdout)
                self.stdout.write("Verifying copy against source:")
                ok = verify(pairs, self.stdout)
                if not ok:
                    raise CommandError("copy does not match source; rolled back")
                if not execute:
                    raise DryRunRollback()
        except DryRunRollback:
            self.stdout.write("Dry run complete: all counts match. Rolled back; nothing was written. "
                              "Re-run with --execute to commit.")
            return
        self.stdout.write("Committed. New project id %d: /ale/project/%d/" % (new_project.pk,
                                                                              new_project.pk))
