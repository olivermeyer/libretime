import math
import telnetlib
import time
from datetime import datetime
from queue import Queue
from threading import Thread

from loguru import logger

from ..config import PUSH_INTERVAL, Config
from ..timeout import ls_timeout
from .queue import PypoLiqQueue


def is_stream(media_item):
    return media_item["type"] == "stream_output_start"


def is_file(media_item):
    return media_item["type"] == "file"


class PypoPush(Thread):
    name = "push"

    def __init__(self, q, telnet_lock, pypo_liquidsoap, config: Config):
        Thread.__init__(self)
        self.queue = q

        self.telnet_lock = telnet_lock
        self.config = config

        self.pushed_objects = {}
        self.current_prebuffering_stream_id = None
        self.queue_id = 0

        self.future_scheduled_queue = Queue()
        self.pypo_liquidsoap = pypo_liquidsoap

        self.plq = PypoLiqQueue(self.future_scheduled_queue, self.pypo_liquidsoap)
        self.plq.daemon = True
        self.plq.start()

    def main(self):
        loops = 0
        heartbeat_period = math.floor(30 / PUSH_INTERVAL)

        media_schedule = None

        while True:
            try:
                media_schedule = self.queue.get(block=True)
            except Exception as exception:
                logger.exception(exception)
                raise exception
            else:
                logger.debug(media_schedule)
                # separate media_schedule list into currently_playing and
                # scheduled_for_future lists
                currently_playing, scheduled_for_future = self.separate_present_future(
                    media_schedule
                )

                self.pypo_liquidsoap.verify_correct_present_media(currently_playing)
                self.future_scheduled_queue.put(scheduled_for_future)

            if loops % heartbeat_period == 0:
                logger.info("heartbeat")
                loops = 0
            loops += 1

    def separate_present_future(self, media_schedule):
        tnow = datetime.utcnow()

        present = []
        future = {}

        sorted_keys = sorted(media_schedule.keys())
        for mkey in sorted_keys:
            media_item = media_schedule[mkey]

            # Ignore track that already ended
            if media_item["type"] == "file" and media_item["end"] < tnow:
                logger.debug(f"ignoring ended media_item: {media_item}")
                continue

            diff_sec = (tnow - media_item["start"]).total_seconds()

            if diff_sec >= 0:
                logger.debug(f"adding media_item to present: {media_item}")
                present.append(media_item)
            else:
                logger.debug(f"adding media_item to future: {media_item}")
                future[mkey] = media_item

        return present, future

    @ls_timeout
    def stop_web_stream_all(self):
        try:
            self.telnet_lock.acquire()
            tn = telnetlib.Telnet(
                self.config.playout.liquidsoap_host,
                self.config.playout.liquidsoap_port,
            )

            # msg = 'dynamic_source.read_stop_all xxx\n'
            msg = "http.stop\n"
            logger.debug(msg)
            tn.write(msg)

            msg = "dynamic_source.output_stop\n"
            logger.debug(msg)
            tn.write(msg)

            msg = "dynamic_source.id -1\n"
            logger.debug(msg)
            tn.write(msg)

            tn.write("exit\n")
            logger.debug(tn.read_all())

        except Exception as exception:
            logger.exception(exception)
        finally:
            self.telnet_lock.release()

    def run(self):
        while True:
            try:
                self.main()
            except Exception as exception:
                logger.exception(exception)
                time.sleep(5)
