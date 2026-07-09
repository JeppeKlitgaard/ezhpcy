from dtuhpc.detect._host_type import (
    am_compute_node as am_compute_node,
    am_login_node as am_login_node,
    am_other as am_other,
    get_host_type as get_host_type,
    HostType as HostType,
)

from dtuhpc.detect._scheduler_type import (
    am_lsf as am_lsf,
    get_scheduler_type as get_scheduler_type,
    SchedulerType as SchedulerType,
)
