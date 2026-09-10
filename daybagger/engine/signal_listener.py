import sys
import zmq
from datetime import datetime, timezone
from pathlib import Path
from trading_contracts.schemas.v1 import SignalCandidate

IPC_ENDPOINT = (
    "ipc:///home/ubuntu/run/signals.ipc"
    if Path("/home/ubuntu/run").exists()
    else "ipc:///tmp/signals.ipc"
)


def start_listener(risk_gate_callback=None):
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(IPC_ENDPOINT)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    print(f"[*] Daybagger listener attached to {IPC_ENDPOINT}")

    while True:
        try:
            topic, payload = socket.recv_multipart()
            signal = SignalCandidate.model_validate_json(payload.decode("utf-8"))
            now = datetime.now(timezone.utc)

            # Drop expired signals instantly
            if now > signal.valid_until:
                print(f"[REJECT-STALE] Signal {signal.signal_id[:8]} expired at {signal.valid_until}")
                continue

            print(f"[ACCEPT-SIGNAL] {signal.direction.value} {signal.instrument_id} @ trigger {signal.entry_trigger} | Regime: {signal.regime.value}")

            # Hook into Daybagger's existing tradeability & risk evaluation
            if risk_gate_callback:
                risk_gate_callback(signal)

        except Exception as e:
            print(f"[ERROR] Listener failure: {e}", file=sys.stderr)


if __name__ == "__main__":
    start_listener()
