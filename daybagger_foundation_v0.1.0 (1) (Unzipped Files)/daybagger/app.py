import logging
from pathlib import Path
from daybagger.bootstrap import verify_golden_rules
from daybagger.config import load_settings
from daybagger.logging_utils import configure_logging
from daybagger.storage.sqlite_store import SQLiteControlStore

def run_foundation_boot(*, repo_root:Path, config_path:Path|None=None)->int:
    repo_root=repo_root.resolve(); config_path=config_path or repo_root/'config'/'default.toml'
    rules=verify_golden_rules(repo_root); settings=load_settings(config_path); configure_logging(settings.app.log_level)
    log=logging.getLogger('daybagger')
    log.info('golden_rules_verified',extra={'event_data':{'path':str(rules.path),'sha256':rules.sha256,'bytes':rules.bytes_count}})
    db_path=Path(settings.storage.control_db_path); db_path=db_path if db_path.is_absolute() else repo_root/db_path
    store=SQLiteControlStore(db_path); store.initialize(); store.record_boot(app_name=settings.app.name,environment=settings.app.environment,trading_mode=settings.app.trading_mode,goldenrules_sha256=rules.sha256)
    store.record_event('FOUNDATION_BOOT',{'status':'READY','trading_mode':settings.app.trading_mode,'strategy_logic_loaded':False,'live_execution_enabled':False})
    log.info('foundation_ready',extra={'event_data':{'trading_mode':settings.app.trading_mode,'strategy_logic_loaded':False,'live_execution_enabled':False,'control_db':str(db_path)}})
    return 0
