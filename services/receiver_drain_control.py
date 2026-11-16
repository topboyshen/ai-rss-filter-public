"""Authenticated bounded receiver stop/transfer control; no open/deploy action."""
from __future__ import annotations

from services import admission_fence as gate
from services.receiver_handoff_control import ControlHandler, ControlServer, ReceiverControl
from services.receiver_drain_transfer import DrainTransfer, body_limit, config_sha256, sources

PROTOCOL = "receiver-durable-original-transfer-v1"


class ReceiverDrainControl(ReceiverControl):
    def drain_registration(self):
        return {"schema": 1, "protocol": PROTOCOL, "receiver": self.registration(),
                "source_sha256": sources(), "body_limit": body_limit(), "config_sha256": config_sha256(),
                "actions": ["transfer", "query"], "switch_authorized": False, "queue_checked": False}

    def transfer(self, value):
        self.registration()
        return {"registration": self.drain_registration(), "receipt": DrainTransfer(self.fence).prepare(value)}

    def transfer_query(self, operation_id):
        self.registration()
        return {"registration": self.drain_registration(), "receipt": DrainTransfer(self.fence).query(operation_id)}


class DrainHandler(ControlHandler):
    def do_GET(self):
        if not self.path.startswith("/drain/"):
            return super().do_GET()
        if not self._authorized():
            return
        try:
            gate._require(not self.headers.get_all("Transfer-Encoding")
                          and self.headers.get_all("Content-Length") in (None, ["0"]), "unexpected body")
            if self.path == "/drain/registration":
                value = self.server.control.drain_registration()
            else:
                prefix = "/drain/operations/"
                gate._require(self.path.startswith(prefix) and gate.HEX64.fullmatch(self.path[len(prefix):]),
                              "exact transfer query required")
                value = self.server.control.transfer_query(self.path[len(prefix):])
            self._reply(200, value)
        except Exception:
            self._reply(409, {"error": "transfer_unconfirmed", "durable": False})

    def do_POST(self):
        if not self.path.startswith("/drain/"):
            return super().do_POST()
        if not self._authorized():
            return
        try:
            lengths = self.headers.get_all("Content-Length") or []
            gate._require(self.path == "/drain/transfer" and not self.headers.get_all("Transfer-Encoding")
                          and len(lengths) == 1 and lengths[0].isascii() and lengths[0].isdecimal()
                          and 0 < int(lengths[0]) <= 8192
                          and self.headers.get_all("Content-Type") == ["application/json"], "exact bounded transfer request required")
            raw = self.rfile.read(int(lengths[0]))
            gate._require(len(raw) == int(lengths[0]), "incomplete transfer request")
            self._reply(200, self.server.control.transfer(gate._parsed(raw)))
        except Exception:
            self._reply(409, {"error": "transfer_unconfirmed", "durable": False})


class DrainServer(ControlServer):
    def finish_request(self, request, client_address):
        DrainHandler(request, client_address, self)


def main():
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()
    token = gate._read(args.token_file)
    gate._require(isinstance(token, dict) and set(token) == {"token"}, "exact private token required")
    fence = gate._configured()
    gate._require(fence is not None, "existing enforced fence required")
    with DrainServer(ReceiverDrainControl(fence), token["token"], args.port) as server:
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
