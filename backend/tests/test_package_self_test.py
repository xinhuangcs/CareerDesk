"""Runtime assets required by every frozen desktop package."""

from careerdesk.bootstrap.package_self_test import run_package_self_test


def test_package_runtime_dependencies_load_from_active_installation():
    run_package_self_test()
