def test_foundation_imports():
    import daybagger, daybagger.app, daybagger.bootstrap, daybagger.config, daybagger.data.base, daybagger.domain, daybagger.execution.base, daybagger.execution.paper, daybagger.intelligence.base, daybagger.learning.base, daybagger.meta.base, daybagger.models.base, daybagger.risk.base, daybagger.storage.sqlite_store
    assert daybagger.__version__=='0.1.0'
