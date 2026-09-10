import sys
import zmq
from datetime import datetime, timezone, timedelta
from pathlib import Path
from trading_contracts.schemas.v1 import SignalCandidate
from daybagger.engine.risk_gate import RiskDesk
from daybagger.engine.order_desk import PaperOrderDesk

IPC_ENDPOINT = (
    "ipc:///home/ubuntu/run/signals.ipc"
    if Path("/home/ubuntu/run").exists()
    else "ipc:///tmp/signals.ipc"
)
IST_OFFSET = timedelta(hours=5, minutes=30)


def start_listener():
    context = zmq.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(IPC_ENDPOINT)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    print(f"[*] Daybagger Listening on {IPC_ENDPOINT}")

    risk_desk = RiskDesk()
    order_desk = PaperOrderDesk()
    latest_prices = {}

    while True:
        try:
            topic, payload = socket.recv_multipart()
            signal = SignalCandidate.model_validate_json(payload.decode("utf-8"))
            now_utc = datetime.now(timezone.utc)
            now_ist = now_utc + IST_OFFSET

            if signal.entry_trigger and signal.entry_trigger > 0:
                latest_prices[signal.instrument_id] = signal.entry_trigger

            # Evaluate exits and square-offs for existing positions
            order_desk.evaluate_open_positions(latest_prices, now_ist)

            # Block new entries after 14:45 IST or expired setups
            if now_ist.hour >= 15 or (now_ist.hour == 14 and now_ist.minute > 45):
                continue

            if now_utc > signal.valid_until:
                continue

            eval_result = risk_desk.evaluate(signal)
            order_desk.record_signal(signal, eval_result)

        except Exception as e:
            print(f"[ERROR] Listener loop exception: {e}", file=sys.stderr)


if __name__ == "__main__":
    start_listener()
