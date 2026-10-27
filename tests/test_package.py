from importlib.metadata import version

import gigareview


def test_package_imports_and_is_installed() -> None:
    assert gigareview.__doc__
    assert version("gigareview")
