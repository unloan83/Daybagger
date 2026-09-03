import importlib.util

def test_foundation_imports():
    import daybagger
    import daybagger.app
    import daybagger.bootstrap
    import daybagger.config
    import daybagger.data.base
    import daybagger.domain
    import daybagger.execution.base
    import daybagger.execution.paper
    import daybagger.intelligence.base
    import daybagger.intelligence.meta_features
    import daybagger.meta.forest
    import daybagger.meta.stack
    import daybagger.storage.sqlite_store

    assert daybagger.__version__ == "0.1.0"


def test_only_meta_decision_engine_remains_authoritative():
    assert importlib.util.find_spec("daybagger.integration.engine") is None
    assert importlib.util.find_spec("daybagger.decision.ensemble") is None
