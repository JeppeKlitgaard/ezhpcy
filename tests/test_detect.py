from unittest.mock import patch

from ezhpcy.detect.host_type import HostType, get_host_type
from ezhpcy.detect.scheduler_type import get_scheduler_type
from ezhpcy.scheduler.types import SchedulerType


def test_pbs_job_environment_takes_precedence_over_lsf_installation() -> None:
    environment = {"PBS_JOBID": "42.server", "LSF_ENVDIR": "/lsf/conf"}

    with patch.dict("os.environ", environment, clear=True):
        assert get_scheduler_type() is SchedulerType.PBS


def test_pbs_job_environment_marks_non_login_host_as_compute_node() -> None:
    with patch.dict("os.environ", {"PBS_JOBID": "42.server"}, clear=True):
        assert get_host_type() is HostType.COMPUTE_NODE


def test_scheduler_type_is_none_outside_a_scheduler_job() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert get_scheduler_type() is None
