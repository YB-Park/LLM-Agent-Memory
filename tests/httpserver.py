"""Run the real ASGI app on a real socket (uvicorn in a thread) for protocol-level tests."""
import dataclasses
import threading
import time

import uvicorn

from memory_server.app import create_app


class RunningServer(object):
    def __init__(self, config, **overrides):
        self.config = dataclasses.replace(config, allowed_hosts=("127.0.0.1", "localhost"), **overrides)
        self.app = create_app(self.config)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning",
                                                    access_log=False, lifespan="on"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        else:
            raise RuntimeError("server did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = "http://127.0.0.1:%d" % self.port
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)
