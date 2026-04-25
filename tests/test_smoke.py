def test_imports():
    import cascade
    import cascade.config

    assert cascade.__version__
    s = cascade.config.Settings()
    assert s.cascade_max_iterations >= 3
