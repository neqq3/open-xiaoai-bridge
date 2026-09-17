"""设备适配描述；实验型号必须显式选择，不能绕过型号和固件检查。"""
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class NativeVisualProfile:
    name: str
    model: str
    firmware: str
    phase_script: str
    microphone_relay: bool
    experimental: bool = False


OH2P = NativeVisualProfile('oh2p_1.62.2', 'OH2P', '1.62.2', 'oh2p_visual_phase.sh', True)
LX06 = NativeVisualProfile('lx06_1.94.13_experimental', 'LX06', '1.94.13', 'lx06_visual_phase.sh', False, True)


def select_profile(identity: str, requested: str = 'auto') -> NativeVisualProfile:
    """只识别明确的型号和 banner 版本；auto 不启用尚未实机验证的设备。"""
    lines = identity.splitlines()
    version = re.search(r'\bVer:(\d+\.\d+\.\d+)\s', identity)
    if not lines or version is None:
        raise ValueError('unknown native visual device identity')
    candidates = (OH2P,) if requested == 'auto' else tuple(p for p in (OH2P, LX06) if p.name == requested)
    for profile in candidates:
        if lines[0] == profile.model and version.group(1) == profile.firmware:
            return profile
    raise ValueError('unsupported native visual profile for this model/firmware')
