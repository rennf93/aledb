# Data curation commands

Two management commands support curating experiment metadata in place without
losing the original. Both default to a **dry run** that performs every write
inside a transaction, verifies the result, and rolls back; `--execute` commits.
Run them inside the web container:

```
sudo docker exec aledb-web python manage.py <command> ...
```

## Convention

1. **Back up first.** Take a `mysqldump` before any curation run (see
   `docs/operations/` for the dump method); keep it outside the repo.
2. **Never curate the original.** Clone the project, curate the clone, and leave
   the source project untouched until the team signs off.
3. **Dry-run, review the printed plan, then execute.** Keep the dry-run and
   execute logs with the backup, outside the repo (they contain private
   experiment detail).
4. **Document in the database, not only in files.** Both commands write a dated
   line to `AleExperiment.notes`; `renumber_flasks` also writes `Flask.comments`.
   Project-level context goes in `Project.description`. Both fields are shown on
   the project page and edited in Django admin.
5. **Run artifacts stay private.** Only the generic commands and this page are
   committed; the CSVs, logs and source documents describing a private
   experiment are not.

## `clone_project` — deep-copy a project

```
manage.py clone_project <project_id> <new_name> [--owner USER] [--description TEXT] [--execute]
```

Creates a new, private project and copies every experiment and everything under
it (ALEs, flasks, isolates, technical replicates, sequencing samples, observed
mutations, missing-coverage evidence, enrichment / fixation / convergence rows,
per-experiment filters, publications) with every foreign key remapped. Object
permissions on the project are copied. The shared mutation catalog, media,
freezer boxes and instruments are referenced, not copied. Breseq report paths
are copied as text, so the clone's report links point at the original files.
The Stats page cache (`stats.StaticData`, keyed by experiment id without a
foreign key) is rebuilt for each new experiment. Experiment dates are preserved.

The verify step compares per-experiment row counts and the full list of sample
names between source and copy, and fails the run on any difference.

## `renumber_flasks` — correct flask numbers from a CSV

```
manage.py renumber_flasks <changes.csv> --source "<where the numbers came from>" [--execute] [--audit-csv PATH] [--date YYYY-MM-DD]
```

CSV columns: `experiment_id, ale_number, old_flask_number, new_flask_number`
(header required; extra columns ignored).

The displayed sample name `A<ale> F<flask> I<isolate> R<rep>` is built live from
the number fields, so changing a flask number renames every sample in that
flask across the site. The command checks that every experiment, ALE and flask
exists, that no two rows target one flask, and that the resulting flask numbers
in each ALE stay unique. It renumbers in two phases (old → negative placeholder
→ new) inside one transaction so chains and swaps never trip the unique
constraint, then verifies the resulting sample-name set against the plan.

Side effects to be aware of:

- Breseq report folders on disk are named with the numbers used at upload and
  are not renamed; the report link of a renamed sample still opens the old
  folder. The experiment note written by the command says so.
- Nothing else references the flask number: the `sample_name` text field on
  sequencing rows is only set by newer uploads, and cached statistics do not
  include flask numbers.

**Undo:** swap the `old_flask_number` / `new_flask_number` columns of the audit
CSV and run again with `--execute`, or restore the flask rows from the backup.
