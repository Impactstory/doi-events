import argparse
import os
from pprint import pprint
from subprocess import call
from time import sleep, time

import heroku3
import sentry_sdk

import jobs_defs  # needs to be imported so the definitions get loaded into the registry
from app import HEROKU_APP_NAME, db, logger
from jobs import update_registry
from util import clean_doi, elapsed, get_sql_answer, get_sql_answers, run_sql

sentry_sdk.init(os.environ.get("SENTRY_DSN"))


def monitor_till_done(job_type):
    logger.info("collecting data. will have some stats soon...")
    logger.info("\n\n")

    num_total = number_total_on_queue(job_type)
    print("num_total", num_total)
    num_unfinished = number_unfinished(job_type)
    print("num_unfinished", num_unfinished)

    loop_thresholds = {"short": 30, "long": 10 * 60, "medium": 60}
    loop_unfinished = {"short": num_unfinished, "long": num_unfinished}
    loop_start_time = {"short": time(), "long": time()}

    while all(loop_unfinished.values()):
        for loop in ["short", "long"]:
            if elapsed(loop_start_time[loop]) > loop_thresholds[loop]:
                if loop in ["short", "long"]:
                    num_unfinished_now = number_unfinished(job_type)
                    num_finished_this_loop = loop_unfinished[loop] - num_unfinished_now
                    loop_unfinished[loop] = num_unfinished_now
                    if loop == "long":
                        logger.info("\n****"),
                    logger.info(
                        "   {} finished in the last {} seconds, {} of {} are now finished ({}%).  ".format(
                            num_finished_this_loop,
                            loop_thresholds[loop],
                            num_total - num_unfinished_now,
                            num_total,
                            int(
                                100 * float(num_total - num_unfinished_now) / num_total
                            ),
                        )
                    ),  # comma so the next part will stay on the same line
                    if num_finished_this_loop:
                        minutes_left = (
                            float(num_unfinished_now)
                            / num_finished_this_loop
                            * loop_thresholds[loop]
                            / 60
                        )
                        logger.info(
                            "{} estimate: done in {} mins, which is {} hours".format(
                                loop,
                                round(minutes_left, 1),
                                round(minutes_left / 60, 1),
                            )
                        )
                    else:
                        print()
                    loop_start_time[loop] = time()
                    # print_idle_dynos(job_type)
        print(".", end=" ")
        sleep(3)
    logger.info("everything is done.  turning off all the dynos")
    scale_dyno(0, job_type)


def number_total_on_queue(job_type):
    num = get_sql_answer(db, "select count(*) from {}".format(table_name(job_type)))
    return num


def number_waiting_on_queue(job_type):
    num = get_sql_answer(
        db, "select count(*) from {} where started is null".format(table_name(job_type))
    )
    return num


def number_unfinished(job_type):
    num = get_sql_answer(
        db,
        "select count(*) from {} where finished is null".format(table_name(job_type)),
    )
    return num


def print_status(job_type):
    num_dois = number_total_on_queue(job_type)
    num_waiting = number_waiting_on_queue(job_type)
    if num_dois:
        logger.info(
            "There are {} dois in the queue, of which {} ({}%) are waiting to run".format(
                num_dois, num_waiting, int(100 * float(num_waiting) / num_dois)
            )
        )


def kick(job_type):
    q = """update {table_name} set started=null, finished=null
          where finished is null
          and id in (select id from {table_name} where started is not null)""".format(
        table_name=table_name(job_type)
    )
    run_sql(db, q)
    print_status(job_type)


def reset_enqueued(job_type):
    q = "update {} set started=null, finished=null".format(table_name(job_type))
    run_sql(db, q)


def truncate(job_type):
    q = "truncate table {}".format(table_name(job_type))
    run_sql(db, q)


def table_name(job_type):
    table_name = "doi_queue_paperbuzz"
    if job_type == "hybrid":
        table_name += "_with_hybrid"
    elif job_type == "dates":
        table_name += "_dates"
    return table_name


def process_name(job_type):
    process_name = "run"  # formation name is from Procfile
    if job_type == "hybrid":
        process_name += "_with_hybrid"
    elif job_type == "dates":
        process_name += "_dates"
    return process_name


def num_dynos(job_type):
    heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
    num_dynos = 0
    try:
        dynos = heroku_conn.apps()[HEROKU_APP_NAME].dynos()[process_name(job_type)]
        num_dynos = len(dynos)
    except (KeyError, TypeError) as e:
        pass
    return num_dynos


def print_idle_dynos(job_type):
    heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
    app = heroku_conn.apps()[HEROKU_APP_NAME]
    running_dynos = []
    try:
        running_dynos = [
            dyno for dyno in app.dynos() if dyno.name.startswith(process_name(job_type))
        ]
    except (KeyError, TypeError) as e:
        pass

    dynos_still_working = get_sql_answers(
        db,
        "select dyno from {} where started is not null and finished is null".format(
            table_name(job_type)
        ),
    )
    dynos_still_working_names = [n for n in dynos_still_working]

    logger.info(
        "dynos still running: {}".format(
            [d.name for d in running_dynos if d.name in dynos_still_working_names]
        )
    )
    # logger.info(u"dynos stopped:", [d.name for d in running_dynos if d.name not in dynos_still_working_names])
    # kill_list = [d.kill() for d in running_dynos if d.name not in dynos_still_working_names]


def scale_dyno(n, job_type):
    logger.info("starting with {} dynos".format(num_dynos(job_type)))
    logger.info("setting to {} dynos".format(n))
    heroku_conn = heroku3.from_key(os.getenv("HEROKU_API_KEY"))
    app = heroku_conn.apps()[HEROKU_APP_NAME]
    app.process_formation()[process_name(job_type)].scale(n)

    logger.info("sleeping for 2 seconds while it kicks in")
    sleep(2)
    logger.info("verifying: now at {} dynos".format(num_dynos(job_type)))


def print_logs(job_type):
    command = "heroku logs -t | grep {}".format(process_name(job_type))
    call(command, shell=True)


def add_dois_to_queue_from_file(filename, job_type):
    start = time()

    command = """psql `heroku config:get DATABASE_URL`?ssl=true -c "\copy {table_name} (id) FROM '{filename}' WITH CSV DELIMITER E'|';" """.format(
        table_name=table_name(job_type), filename=filename
    )
    call(command, shell=True)

    q = "update {} set id=lower(id)".format(table_name(job_type))
    run_sql(db, q)

    logger.info(
        "add_dois_to_queue_from_file done in {} seconds".format(elapsed(start, 1))
    )
    print_status(job_type)


def add_dois_to_queue_from_query(where, job_type):
    logger.info("adding all dois, this may take a while")
    start = time()

    # run_sql(db, "drop table {} cascade".format(table_name(job_type)))
    # create_table_command = "CREATE TABLE {} as (select id, random() as rand, false as enqueued, null::timestamp as finished, null::timestamp as started, null::text as dyno from crossref)".format(
    #     table_name(job_type))
    create_table_command = "CREATE TABLE {} as (select doi as id, random() as rand, false as enqueued, null::timestamp as finished, null::timestamp as started, null::text as dyno from dois_wos_stefi)".format(
        table_name(job_type)
    )

    if where:
        create_table_command = create_table_command.replace(
            "from crossref)", "from crossref where {})".format(where)
        )
    run_sql(db, create_table_command)
    recreate_commands = """
        alter table {table_name} alter column rand set default random();
        CREATE INDEX {table_name}_id_idx ON {table_name} USING btree (id);
        CREATE INDEX {table_name}_finished_null_rand_idx on {table_name} (rand) where finished is null;
        CREATE INDEX {table_name}_started_null_rand_idx ON {table_name} USING btree (rand, started) WHERE started is null;
        -- from https://lob.com/blog/supercharge-your-postgresql-performance
        -- vacuums and analyzes every ten million rows
        ALTER TABLE {table_name} SET (autovacuum_vacuum_scale_factor = 0.0);
        ALTER TABLE {table_name} SET (autovacuum_vacuum_threshold = 10000000);
        ALTER TABLE {table_name} SET (autovacuum_analyze_scale_factor = 0.0);
        ALTER TABLE {table_name} SET (autovacuum_analyze_threshold = 10000000);
        """.format(
        table_name=table_name(job_type)
    )
    for command in recreate_commands.split(";"):
        run_sql(db, command)

    command = """create or replace view export_queue as
     SELECT id AS doi,
        updated AS updated,
        response_jsonb->>'evidence' AS evidence,
        response_jsonb->>'oa_status' AS oa_color,
        response_jsonb->>'free_fulltext_url' AS best_open_url,
        response_jsonb->>'year' AS year,
        response_jsonb->>'found_hybrid' AS found_hybrid,
        response_jsonb->>'found_green' AS found_green,
        response_jsonb->>'error' AS error,
        response_jsonb->>'is_boai_license' AS is_boai_license,
        replace(api->'_source'->>'journal', '
    ', '') AS journal,
        replace(api->'_source'->>'publisher', '
    ', '') AS publisher,
        api->'_source'->>'title' AS title,
        api->'_source'->>'subject' AS subject,
        response_jsonb->>'green_base_collections' AS green_base_collections,
        response_jsonb->>'_open_base_ids' AS open_base_ids,
        response_jsonb->>'_closed_base_ids' AS closed_base_ids,
        response_jsonb->>'license' AS license
       FROM crossref where id in (select id from {table_name})""".format(
        table_name=table_name(job_type)
    )

    run_sql(db, command)

    # they are already lowercased
    logger.info(
        "add_dois_to_queue_from_query done in {} seconds".format(elapsed(start, 1))
    )
    print_status(job_type)


def run(parsed_args, job_type):
    start = time()
    if job_type in ("normal", "hybrid"):
        update = update_registry.get("WeeklyStats." + process_name(job_type))
        if parsed_args.doi:
            parsed_args.id = clean_doi(parsed_args.doi)
            parsed_args.doi = None
    else:
        update = update_registry.get("DateRange.get_events")

    update.run(**vars(parsed_args))

    logger.info("finished update in {} seconds".format(elapsed(start)))

    if job_type in ("normal", "hybrid"):
        from event import CedEvent

        my_event = CedEvent.query.get(parsed_args.id)
        pprint(my_event)


# python doi_queue.py --hybrid --filename=data/dois_juan_accuracy.csv --dynos=40 --soup


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run stuff.")
    parser.add_argument(
        "--id",
        nargs="?",
        type=str,
        help="id of the one thing you want to update (case sensitive)",
    )
    parser.add_argument(
        "--doi",
        nargs="?",
        type=str,
        help="id of the one thing you want to update (case insensitive)",
    )

    parser.add_argument(
        "--filename", nargs="?", type=str, help="filename with dois, one per line"
    )
    parser.add_argument(
        "--addall", default=False, action="store_true", help="add everything"
    )
    parser.add_argument(
        "--where",
        nargs="?",
        type=str,
        default=None,
        help="""where string for addall (eg --where="response_jsonb->>'oa_status'='green'")""",
    )

    parser.add_argument(
        "--hybrid",
        default=False,
        action="store_true",
        help="if hybrid, else don't include",
    )
    parser.add_argument(
        "--dates", default=False, action="store_true", help="use date queue"
    )
    parser.add_argument(
        "--all", default=False, action="store_true", help="do everything"
    )

    parser.add_argument(
        "--view", nargs="?", type=str, default=None, help="view name to export from"
    )

    parser.add_argument(
        "--reset", default=False, action="store_true", help="do you want to just reset?"
    )
    parser.add_argument(
        "--run", default=False, action="store_true", help="to run the queue"
    )
    parser.add_argument(
        "--status", default=False, action="store_true", help="to logger.info(the status"
    )
    parser.add_argument(
        "--dynos", default=None, type=int, help="scale to this many dynos"
    )
    parser.add_argument(
        "--export", default=False, action="store_true", help="export the results"
    )
    parser.add_argument(
        "--logs", default=False, action="store_true", help="logger.info(out logs"
    )
    parser.add_argument(
        "--monitor",
        default=False,
        action="store_true",
        help="monitor till done, then turn off dynos",
    )
    parser.add_argument(
        "--soup", default=False, action="store_true", help="soup to nuts"
    )
    parser.add_argument(
        "--kick",
        default=False,
        action="store_true",
        help="put started but unfinished dois back to unstarted so they are retried",
    )

    parsed_args = parser.parse_args()
    job_type = "normal"
    if parsed_args.hybrid:
        job_type = "hybrid"
    if parsed_args.dates:
        job_type = "dates"

    if parsed_args.filename:
        if num_dynos(job_type) > 0:
            scale_dyno(0, job_type)
        truncate(job_type)
        add_dois_to_queue_from_file(parsed_args.filename, job_type)

    if parsed_args.addall or parsed_args.where:
        if num_dynos(job_type) > 0:
            scale_dyno(0, job_type)
        add_dois_to_queue_from_query(parsed_args.where, job_type)

    if parsed_args.soup:
        if num_dynos(job_type) > 0:
            scale_dyno(0, job_type)
        if parsed_args.dynos:
            scale_dyno(parsed_args.dynos, job_type)
        else:
            logger.info("no number of dynos specified, so setting 1")
            scale_dyno(1, job_type)
        monitor_till_done(job_type)
        scale_dyno(0, job_type)
        # export(parsed_args.all, job_type, parsed_args.filename, parsed_args.view)
    else:
        if parsed_args.dynos != None:  # to tell the difference from setting to 0
            scale_dyno(parsed_args.dynos, job_type)
            # if parsed_args.dynos > 0:
            #     print_logs(job_type)

    if parsed_args.reset:
        reset_enqueued(job_type)

    if parsed_args.status:
        print_status(job_type)

    if parsed_args.monitor:
        monitor_till_done(job_type)
        scale_dyno(0, job_type)

    if parsed_args.logs:
        print_logs(job_type)

    # if parsed_args.export:
    #     export_crossref(parsed_args.all, job_type, parsed_args.filename, parsed_args.view)

    if parsed_args.kick:
        kick(job_type)

    if parsed_args.id or parsed_args.doi or parsed_args.run:
        run(parsed_args, job_type)
