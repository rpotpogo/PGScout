import codecs
import logging
import signal
import sys
import time
from Queue import PriorityQueue
from threading import Thread

import math
from flask import Flask, request, jsonify, url_for

from pgscout.ScoutGuard import ScoutGuard
from pgscout.ScoutJob import ScoutJob
from pgscout.cache import get_cached_encounter, cache_encounter, cleanup_cache, get_cached_count
from pgscout.config import cfg_get, cfg_init, get_pokemon_name
from pgscout.console import print_status, hr_tstamp
from pgscout.utils import normalize_encounter_id, \
    load_pgpool_accounts, app_state, rss_mem_size, get_pokemon_prio, PRIO_HIGH, PRIO_LOW, PRIO_NAMES

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [%(threadName)16s][%(module)14s][%(levelname)8s] %(message)s')

log = logging.getLogger(__name__)

# Silence some loggers
logging.getLogger('pgoapi').setLevel(logging.WARNING)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)

scouts = []
jobs = PriorityQueue()

# ===========================================================================


def have_active_scouts():
    for s in scouts:
        if s.active:
            return True
    return False


def reject(reason):
    log.warning(reason)
    return jsonify({
        'success': False,
        'error': reason
    })


@app.route("/iv", methods=['GET'])
def get_iv():
    if not app_state.accept_new_requests:
        return reject('Not accepting new requests.')
    if not have_active_scouts():
        return reject('No active scout available. All banned?')

    pokemon_id = request.args["pokemon_id"]
    pokemon_name = get_pokemon_name(pokemon_id)
    forced = request.args.get('forced')
    prio = PRIO_HIGH if forced is not None else get_pokemon_prio(pokemon_id)

    max_queued_jobs = cfg_get('max_queued_jobs')
    num_jobs = jobs.qsize()
    if max_queued_jobs and num_jobs >= max_queued_jobs and prio == PRIO_LOW:
        return reject(
            "Job queue full ({} items). Rejecting encounter with priority '{}'.".format(num_jobs, PRIO_NAMES[prio]))

    lat = request.args["latitude"]
    lng = request.args["longitude"]
    weather = request.args.get("weather", "unknown")

    encounter_id = normalize_encounter_id(request.args.get("encounter_id"))
    # Spawn point ID is assumed to be a hex string
    spawn_point_id = request.args.get("spawn_point_id")
    despawn_time = request.args.get("despawn_time")

    # Check cache
    cache_key = "{}-{}".format(encounter_id, weather) if encounter_id else "{}-{}-{}".format(pokemon_id, lat, lng)
    result = get_cached_encounter(cache_key)
    if result:
        log.info(
            u"Returning cached result: {:.1f}% level {} {} with {} CP".format(result['iv_percent'], result['level'], pokemon_name, result['cp']))
        return jsonify(result)

    # Create a ScoutJob
    job = ScoutJob(pokemon_id, encounter_id, spawn_point_id, lat, lng, despawn_time=despawn_time, ditto_mode=False)

    # Enqueue and wait for job to be processed
    jobs.put((prio, time.time(), job))
    while not job.processed:
        time.sleep(1)

    # Cache successful jobs and return result
    if job.result['success']:
        cache_encounter(cache_key, job.result)
    return jsonify(job.result)

@app.route("/ditto", methods=['GET'])
def get_ditto():
    pokemon_id = request.args["pokemon_id"]
    pokemon_name = get_pokemon_name(pokemon_id)
    forced = request.args.get('forced')
    prio = PRIO_HIGH if forced is not None else get_pokemon_prio(pokemon_id)

    lat = request.args["latitude"]
    lng = request.args["longitude"]

    encounter_id = normalize_encounter_id(request.args.get("encounter_id"))
    # Spawn point ID is assumed to be a hex string
    spawn_point_id = request.args.get("spawn_point_id")
    despawn_time = request.args.get("despawn_time")

    # Create a ScoutJob
    job = ScoutJob(pokemon_id, encounter_id, spawn_point_id, lat, lng, despawn_time=despawn_time, ditto_mode=True)

    # Enqueue and wait for job to be processed
    jobs.put((prio, time.time(), job))
    while not job.processed:
        time.sleep(1)

    return jsonify(job.result)

@app.route("/status/", methods=['GET'])
@app.route("/status/<int:page>", methods=['GET'])
def status(page=1):

    def td(cell):
        return "<td>{}</td>".format(cell)

    max_scouts_per_page = 25
    max_page = int(math.ceil(len(scouts)/float(max_scouts_per_page)))
    lines = "<style> th,td { padding-left: 10px; padding-right: 10px; border: 1px solid #ddd; } table { border-collapse: collapse } td { text-align:center }</style>"
    lines += "<meta http-equiv='Refresh' content='5'>"
    lines += "Accepting requests: {} | Job queue length: {} | Cached encounters: {} | Mem Usage: {}".format(
                app_state.accept_new_requests, jobs.qsize(), get_cached_count(), rss_mem_size())
    lines += "<br><br>"

    if cfg_get('proxies'):
        headers = ['#', 'Scout', 'Proxy', 'Start', 'Warn', 'Active', 'Encounters', 'Enc/h', 'Errors',
                             'Last Encounter', 'Message']
    else:
        headers = ['#', 'Scout', 'Start', 'Warn', 'Active', 'Encounters', 'Enc/h', 'Errors',
                                      'Last Encounter', 'Message']

    lines += "<table><tr>"
    for h in headers:
        lines += "<th>{}</th>".format(h)
    lines += "</tr>"

    if page * max_scouts_per_page > len(scouts):    #Page number is too great, set to last page
        page = max_page
    if page < 1:
        page = 1
    for i in range((page-1)*max_scouts_per_page, page*max_scouts_per_page):
        if i >= len(scouts):
            break
        lines += "<tr>"
        s = scouts[i].acc
        warn = s.get_state('warn')
        warn_str = '' if warn is None else ('Yes' if warn else 'No')
        lines += td(i+1)
        lines += td(s.username)
        lines += td(s.proxy_url) if cfg_get('proxies') else ""
        lines += td(hr_tstamp(s.start_time))
        lines += td(warn_str)
        lines += td('Yes' if scouts[i].active else 'No')
        lines += td(s.total_encounters)
        lines += td("{:5.1f}".format(s.encounters_per_hour))
        lines += td(s.errors)
        lines += td(hr_tstamp(s.previous_encounter))
        lines += td(s.last_msg)
        lines += "</tr>"
    lines += "</table>"

    lines += "<br>"
    if len(scouts) > max_scouts_per_page:  # Use pages if we have more than max_scouts_per_page
        lines += "Page: "
        if max_page > 1 and page > 1:
            lines += "<a href={}>&lt;</a> | ".format(url_for('status', page=page-1))
        for p in range(1, max_page+1):
            if p == page:
                lines += str(p)
            else:
                url = url_for('status', page=p)
                lines += "<a href={}>{}</a>".format(url, p)
            if p < max_page:
                lines += " | "
        if max_page > 1 and page < max_page:
            lines += " | <a href={}>&gt;</a>".format(url_for('status', page=page+1))

    return lines


def run_webserver():
    app.run(threaded=True, host=cfg_get('host'), port=cfg_get('port'))


def cache_cleanup_thread():
    while True:
        time.sleep(60)
        num_deleted = cleanup_cache()
        log.info("Cleaned up {} entries from encounter cache.".format(num_deleted))


def load_accounts(jobs):
    accounts_file = cfg_get('accounts_file')

    accounts = []
    if accounts_file:
        log.info("Loading accounts from file {}.".format(accounts_file))
        with codecs.open(accounts_file, mode='r', encoding='utf-8') as f:
            for line in f:
                fields = line.split(",")
                fields = map(unicode.strip, fields)
                accounts.append(ScoutGuard(fields[0], fields[1], fields[2], jobs))
    elif cfg_get('pgpool_url') and cfg_get('pgpool_system_id') and cfg_get('pgpool_num_accounts') > 0:

        acc_json = load_pgpool_accounts(cfg_get('pgpool_num_accounts'), reuse=True)
        if isinstance(acc_json, dict):
            acc_json = [acc_json]

        if len(acc_json) > 0:
            log.info("Loaded {} accounts from PGPool.".format(len(acc_json)))
            for acc in acc_json:
                accounts.append(ScoutGuard(acc['auth_service'], acc['username'], acc['password'], jobs))

    if len(accounts) == 0:
        log.error("Could not load any accounts. Nothing to do. Exiting.")
        sys.exit(1)

    return accounts

def signal_handler(signal, frame):
        print "Exiting"
        sys.exit(0)

# ===========================================================================

log.info("PGScout starting up.")

cfg_init()

scouts = load_accounts(jobs)
for scout in scouts:
    t = Thread(target=scout.run)
    t.daemon = True
    t.start()

# Cleanup cache in background
t = Thread(target=cache_cleanup_thread, name="cache_cleaner")
t.daemon = True
t.start()

# Start thread to print current status and get user input.
t = Thread(target=print_status,
           name='status_printer', args=(scouts, cfg_get('initial_view'), jobs))
t.daemon = True
t.start()

# Launch the webserver
t = Thread(target=run_webserver, name='webserver')
t.daemon = True
t.start()

# Catch SIGINT to exit
signal.signal(signal.SIGINT, signal_handler)
while True:
    time.sleep(1)
