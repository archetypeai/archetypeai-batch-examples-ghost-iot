"""
Helpers for loading and traversing the multi-home WiFi topology.

The on-disk topology lives in `data/topology.json`. This module is the only
place that knows the schema — every other script imports `load_topology()` and
the small accessors below, so adding a home or device only requires editing the
JSON file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TOPOLOGY_JSON = os.path.join(REPO_DIR, "data", "topology.json")


@dataclass(frozen=True)
class Device:
    device_id: str
    type: str
    mac: str
    home_id: str
    home_label: str
    gateway_mac: str
    owner: str | None  # None for shared devices


@dataclass(frozen=True)
class Human:
    name: str
    home_id: str
    home_label: str
    gateway_mac: str
    devices: tuple[Device, ...]


@dataclass(frozen=True)
class Home:
    home_id: str
    label: str
    gateway_mac: str
    humans: tuple[Human, ...]
    shared_devices: tuple[Device, ...]

    @property
    def all_devices(self) -> tuple[Device, ...]:
        out: list[Device] = []
        for h in self.humans:
            out.extend(h.devices)
        out.extend(self.shared_devices)
        return tuple(out)


@dataclass(frozen=True)
class Topology:
    homes: tuple[Home, ...]

    @property
    def all_devices(self) -> tuple[Device, ...]:
        out: list[Device] = []
        for h in self.homes:
            out.extend(h.all_devices)
        return tuple(out)

    @property
    def all_humans(self) -> tuple[Human, ...]:
        out: list[Human] = []
        for h in self.homes:
            out.extend(h.humans)
        return tuple(out)

    def device_by_mac(self, mac: str) -> Device | None:
        for d in self.all_devices:
            if d.mac == mac:
                return d
        return None


def load_topology(path: str = DEFAULT_TOPOLOGY_JSON) -> Topology:
    with open(path, "r") as f:
        raw = json.load(f)

    homes: list[Home] = []
    for h in raw["homes"]:
        humans: list[Human] = []
        for hu in h["humans"]:
            devices = tuple(
                Device(
                    device_id=d["device_id"],
                    type=d["type"],
                    mac=d["mac"],
                    home_id=h["home_id"],
                    home_label=h["label"],
                    gateway_mac=h["gateway_mac"],
                    owner=hu["name"],
                )
                for d in hu["devices"]
            )
            humans.append(
                Human(
                    name=hu["name"],
                    home_id=h["home_id"],
                    home_label=h["label"],
                    gateway_mac=h["gateway_mac"],
                    devices=devices,
                )
            )
        shared = tuple(
            Device(
                device_id=d["device_id"],
                type=d["type"],
                mac=d["mac"],
                home_id=h["home_id"],
                home_label=h["label"],
                gateway_mac=h["gateway_mac"],
                owner=None,
            )
            for d in h["shared_devices"]
        )
        homes.append(
            Home(
                home_id=h["home_id"],
                label=h["label"],
                gateway_mac=h["gateway_mac"],
                humans=tuple(humans),
                shared_devices=shared,
            )
        )

    return Topology(homes=tuple(homes))


if __name__ == "__main__":
    t = load_topology()
    print(f"{len(t.homes)} homes, {len(t.all_humans)} humans, {len(t.all_devices)} devices")
    for home in t.homes:
        print(f"  {home.label} (gw {home.gateway_mac})")
        for hu in home.humans:
            print(f"    {hu.name}: {[d.type for d in hu.devices]}")
        print(f"    shared: {[d.type for d in home.shared_devices]}")
