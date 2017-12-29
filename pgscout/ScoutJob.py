import random

import time

from pgscout.config import cfg_get, get_pokemon_name


class ScoutJob(object):

    def __init__(self, pokemon_id, encounter_id, spawn_point_id, lat, lng, despawn_time=None, ditto_mode=False):
        self.pokemon_id = int(pokemon_id)
        self.pokemon_name = get_pokemon_name(pokemon_id)
        self.encounter_id = encounter_id
        self.spawn_point_id = spawn_point_id
        self.lat = float(lat)
        self.lng = float(lng)
        self.despawn_time = despawn_time
        self.ditto_mode = ditto_mode

        self.processed = False
        self.result = {}

        # Set a time when this job expires if a TTL was given
        ttl = cfg_get('max_job_ttl')
        self.expire_at = time.time() + (ttl * 60) if ttl else None

        # Use fixed random altitude per job
        self.altitude = random.randint(12, 108)

    def expired(self):
        now = time.time()
        return (self.despawn_time and now > self.despawn_time) or (self.expire_at and now > self.expire_at)
