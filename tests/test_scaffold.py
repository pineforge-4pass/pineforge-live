import pineforge_live

def test_package_metadata():
    assert pineforge_live.__version__ == "0.1.0"
    assert pineforge_live.ADAPTER_API_VERSION == 1
