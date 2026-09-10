import os
import time
import subprocess
from datetime import datetime, timezone, timedelta
import duckdb
from trading_contracts.telemetry import send_telegram

IST = timezone(timedelta(hours=5, minutes=30))
LEDGER_PATH = "/opt/daybagger/data/paper_ledger.duckdb"
LAST_HEARTBEAT_TIME = 0
EOD_REPORTED_TODAY = False

def check_systemd_status(service_name: str) -> tuple[bool, str]:
    try:
        res = subprocess.run(["systemctl", "is-active", service_name], capture_output=True, text=True)
        is_active = res.stdout.strip() == "active"
        return is_active, res.stdout.strip()
    except Exception as e:
        return False, str(e)

def get_ledger_stats():
    if not os.path.exists(LEDGER_PATH):
        return {"total": 0, "open": 0, "closed": 0, "pnl": 0.0, "details": []}
    
    try:
        with duckdb.connect(LEDGER_PATH, read_only=True) as conn:
            summary = conn.execute("""
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN status = 'OPEN' THEN 1 END) as open_cnt,
                    COUNT(CASE WHEN status = 'CLOSED' THEN 1 END) as closed_cnt,
                    ROUND(COALESCE(SUM(pnl), 0.0), 2) as total_pnl
                FROM paper_ledger
                WHERE CAST(timestamp AS DATE) = CURRENT_DATE
            """).fetchone()

            breakdown = conn.execute("""
                SELECT status, COALESCE(exit_reason, 'PENDING'), COUNT(*), ROUND(COALESCE(SUM(pnl), 0.0), 2)
                FROM paper_ledger
                WHERE CAST(timestamp AS DATE) = CURRENT_DATE
                GROUP BY status, exit_reason
            """).fetchall()

            return {
                "total": summary[0] if summary else 0,
                "open": summary[1] if summary else 0,
                "closed": summary[2] if summary else 0,
                "pnl": summary[3] if summary else 0.0,
                "details": breakdown
            }
    except Exception as e:
        return {"total": 0, "open": 0, "closed": 0, "pnl": 0.0, "details": [], "error": str(e)}

def run_loop():
    global LAST_HEARTBEAT_TIME, EOD_REPORTED_TODAY
    print("[*] Telemetry & Heartbeat Daemon active...")
    send_telegram("🚀 *Trading Telemetry Online* - 30m throttled updates & critical error watch enabled.")

    while True:
        now_ist = datetime.now(IST)
        current_ts = time.time()
        is_market_hours = (now_ist.hour == 9 and now_ist.minute >= 15) or (10 <= now_ist.hour < 15) or (now_ist.hour == 15 and now_ist.minute <= 30)

        # 1. Critical Failure Check (Fires instantly on crash)
        dual_ok, dual_state = check_systemd_status("dualengine.service")
        day_ok, day_state = check_systemd_status("daybagger.service")

        if not dual_ok or not day_ok:
            alert = f"🚨 *CRITICAL SERVICE FAULT DETECTED*\n\n"
            alert += f"• `dualengine.service`: *{dual_state}*\n"
            alert += f"• `daybagger.service`: *{day_state}*\n"
            alert += f"⏰ Time: {now_ist.strftime('%H:%M:%S')} IST"
            send_telegram(alert)
            # Sleep 3 mins to prevent flooding during a crash loop
            time.sleep(180)
            continue

        # 2. 30-Minute Market Hours Heartbeat
        if is_market_hours and (current_ts - LAST_HEARTBEAT_TIME >= 1800):
            stats = get_ledger_stats()
            pnl_icon = "🟢" if stats["pnl"] >= 0 else "🔴"
            msg = (
                f"⏱️ *Market Status Update ({now_ist.strftime('%H:%M')} IST)*\n\n"
                f"• Engines: `DualEngine` & `Daybagger` *ACTIVE*\n"
                f"• Signals Today: *{stats['total']}*\n"
                f"• Open Positions: *{stats['open']}*\n"
                f"• Closed Trades: *{stats['closed']}*\n"
                f"• Net P&L: {pnl_icon} *₹{stats['pnl']:.2f}*"
            )
            send_telegram(msg)
            LAST_HEARTBEAT_TIME = current_ts

        # 3. Post-Market Automated EOD Report (Fires once at 15:35 IST)
        if now_ist.hour == 15 and now_ist.minute >= 35 and not EOD_REPORTED_TODAY:
            stats = get_ledger_stats()
            pnl_icon = "🟢" if stats["pnl"] >= 0 else "🔴"
            eod_msg = (
                f"📊 *EOD SESSION SUMMARY — {now_ist.strftime('%d %b %Y')}*\n"
                f"═══════════════════════\n"
                f"• Total Processed Signals: *{stats['total']}*\n"
                f"• Realized P&L: {pnl_icon} *₹{stats['pnl']:.2f}*\n\n"
                f"*Breakdown by Status & Exit:*\n"
            )
            if stats["details"]:
                for row in stats["details"]:
                    eod_msg += f"• `{row[0]}` ({row[1]}): *{row[2]} trades* | ₹{row[3]:.2f}\n"
            else:
                eod_msg += "• No trades recorded today.\n"
            
            send_telegram(eod_msg)
            EOD_REPORTED_TODAY = True

        # Reset EOD flag at midnight
        if now_ist.hour == 0 and EOD_REPORTED_TODAY:
            EOD_REPORTED_TODAY = False

        time.sleep(30)

if __name__ == "__main__":
    run_loop()
