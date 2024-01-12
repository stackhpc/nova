#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

from dataclasses import dataclass
import typing as ty

from oslo_log import log as logging

import nova.conf
from nova import exception
from nova.i18n import _
from nova import objects
from nova.virt import hardware
from nova.virt.libvirt.cpu import core

LOG = logging.getLogger(__name__)

CONF = nova.conf.CONF


@dataclass
class Core:
    """Class to model a CPU core as reported by sysfs.

    It may be a physical CPU core or a hardware thread on a shared CPU core
    depending on if the system supports SMT.
    """

    # NOTE(sbauza): ident is a mandatory field.
    # The CPU core id/number
    ident: int

    @property
    def online(self) -> bool:
        return core.get_online(self.ident)

    @online.setter
    def online(self, state: bool) -> None:
        if state:
            core.set_online(self.ident)
        else:
            core.set_offline(self.ident)

    def __hash__(self):
        return hash(self.ident)

    def __eq__(self, other):
        return self.ident == other.ident

    def __str__(self):
        return str(self.ident)

    @property
    def governor(self) -> ty.Optional[str]:
        try:
            return core.get_governor(self.ident)
        # NOTE(sbauza): cpufreq/scaling_governor is not enabled for some OS
        # platforms.
        except exception.FileNotFound:
            return None

    def set_high_governor(self) -> None:
        core.set_governor(self.ident, CONF.libvirt.cpu_power_governor_high)

    def set_low_governor(self) -> None:
        core.set_governor(self.ident, CONF.libvirt.cpu_power_governor_low)


def power_up(instance: objects.Instance) -> None:
    if not CONF.libvirt.cpu_power_management:
        return
    if instance.numa_topology is None:
        return

    cpu_dedicated_set = hardware.get_cpu_dedicated_set_nozero() or set()
    pcpus = instance.numa_topology.cpu_pinning
    powered_up = set()
    for pcpu in pcpus:
        if pcpu in cpu_dedicated_set:
            pcpu = Core(pcpu)
            if CONF.libvirt.cpu_power_management_strategy == 'cpu_state':
                pcpu.online = True
            else:
                pcpu.set_high_governor()
            powered_up.add(str(pcpu))
    LOG.debug("Cores powered up : %s", powered_up)


def power_down(instance: objects.Instance) -> None:
    if not CONF.libvirt.cpu_power_management:
        return
    if instance.numa_topology is None:
        return

    cpu_dedicated_set = hardware.get_cpu_dedicated_set_nozero() or set()
    pcpus = instance.numa_topology.cpu_pinning
    powered_down = set()
    for pcpu in pcpus:
        if pcpu in cpu_dedicated_set:
            pcpu = Core(pcpu)
            if CONF.libvirt.cpu_power_management_strategy == 'cpu_state':
                pcpu.online = False
            else:
                pcpu.set_low_governor()
            powered_down.add(str(pcpu))
    LOG.debug("Cores powered down : %s", powered_down)


def power_down_all_dedicated_cpus() -> None:
    if not CONF.libvirt.cpu_power_management:
        return

    cpu_dedicated_set = hardware.get_cpu_dedicated_set_nozero() or set()
    for pcpu in cpu_dedicated_set:
        pcpu = Core(pcpu)
        if CONF.libvirt.cpu_power_management_strategy == 'cpu_state':
            pcpu.online = False
        else:
            pcpu.set_low_governor()
    LOG.debug("Cores powered down : %s", cpu_dedicated_set)


def validate_all_dedicated_cpus() -> None:
    if not CONF.libvirt.cpu_power_management:
        return
    cpu_dedicated_set = hardware.get_cpu_dedicated_set() or set()
    governors = set()
    cpu_states = set()
    for pcpu in cpu_dedicated_set:
        if (pcpu == 0 and
                CONF.libvirt.cpu_power_management_strategy == 'cpu_state'):
            LOG.warning('CPU0 is in cpu_dedicated_set, but it is not eligible '
                        'for state management and will be ignored')
            continue
        pcpu = Core(pcpu)
        # we need to collect the governors strategy and the CPU states
        try:
            governors.add(pcpu.governor)
        except FileNotFoundError as e:
            # NOTE(gibi): When
            # /sys/devices/system/cpu/cpuX/cpufreq/scaling_governor does
            # not exist it means the host OS does not support any governors.
            # If cpu_state strategy is requested we can ignore this as
            # governors will not be used but if governor strategy is requested
            # we need to report an error and stop as the host is not properly
            # configured
            if CONF.libvirt.cpu_power_management_strategy == 'governor':
                msg = _(
                    "[libvirt]cpu_power_management_strategy is 'governor', "
                    "but the host OS does not support governors for CPU%d"
                    % pcpu.ident
                )
                raise exception.InvalidConfiguration(msg) from e

        cpu_states.add(pcpu.online)
    if CONF.libvirt.cpu_power_management_strategy == 'cpu_state':
        # all the cores need to have the same governor strategy
        if len(governors) > 1:
            msg = _("All the cores need to have the same governor strategy"
                    "before modifying the CPU states. You can reboot the "
                    "compute node if you prefer.")
            raise exception.InvalidConfiguration(msg)
    elif CONF.libvirt.cpu_power_management_strategy == 'governor':
        # all the cores need to be online
        if False in cpu_states:
            msg = _("All the cores need to be online before modifying the "
                    "governor strategy.")
            raise exception.InvalidConfiguration(msg)
