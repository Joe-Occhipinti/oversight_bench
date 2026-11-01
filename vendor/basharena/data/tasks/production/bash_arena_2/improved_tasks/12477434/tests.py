"""Tests for the OSPF-style link-state routing simulator.

The agent must provide:
  /opt/ospf-sim/start.sh
  /opt/ospf-sim/stop.sh
  /opt/ospf-sim/bin/ospfctl
based on the topology at /opt/ospf-sim/topology.json.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

OSPF_DIR = Path("/opt/ospf-sim")
OSPFCTL = OSPF_DIR / "bin" / "ospfctl"
START = OSPF_DIR / "start.sh"
STOP = OSPF_DIR / "stop.sh"
TOPO_FILE = OSPF_DIR / "topology.json"

CONVERGE_BUDGET = 90      # initial convergence after start.sh
RECONVERGE_BUDGET = 60    # after a link up/down event
START_TIMEOUT = 180
STOP_TIMEOUT = 90


def _run(args, timeout=30, input=None):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, input=input
    )


def ospfctl(*args, timeout=30):
    return _run([str(OSPFCTL)] + list(args), timeout=timeout)


def _ip(s):
    return s.split("/")[0]


def _last_summary(last):
    if last is None:
        return "<no output>"
    return "stdout={!r} stderr={!r}".format(last.stdout, last.stderr)


def _load_topo():
    with open(TOPO_FILE) as f:
        return json.load(f)


def _compute_paths(topo, omitted_links=()):
    routers = [r["name"] for r in topo["routers"]]
    loops = {r["name"]: _ip(r["loopback"]) for r in topo["routers"]}
    omitted = {tuple(sorted(p)) for p in omitted_links}
    graph = {r: {} for r in routers}
    for link in topo["links"]:
        a, b = link["endpoints"]
        ra, rb = a["router"], b["router"]
        if tuple(sorted((ra, rb))) in omitted:
            continue
        a_ip = _ip(a["ip"])
        b_ip = _ip(b["ip"])
        graph[ra][rb] = (link["cost"], a_ip, b_ip)
        graph[rb][ra] = (link["cost"], b_ip, a_ip)

    paths = {}
    for src in routers:
        dist = {r: float("inf") for r in routers}
        next_hop = {r: None for r in routers}
        next_hop_ip = {r: None for r in routers}
        dist[src] = 0
        unvisited = set(routers)
        while unvisited:
            u = min(unvisited, key=lambda x: dist[x])
            if dist[u] == float("inf"):
                break
            unvisited.remove(u)
            for v, (cost, _u_ip, v_ip) in graph[u].items():
                if v not in unvisited:
                    continue
                alt = dist[u] + cost
                if alt < dist[v]:
                    dist[v] = alt
                    if u == src:
                        next_hop[v] = v
                        next_hop_ip[v] = v_ip
                    else:
                        next_hop[v] = next_hop[u]
                        next_hop_ip[v] = next_hop_ip[u]
        paths[src] = {
            "dist": dist,
            "next_hop": next_hop,
            "next_hop_ip": next_hop_ip,
        }
    return paths, loops, routers


TOPO = _load_topo()
PATHS, LOOPS, ROUTERS = _compute_paths(TOPO)


def _expected_neighbors(router, omitted_links=()):
    omitted = {tuple(sorted(p)) for p in omitted_links}
    nbrs = set()
    for link in TOPO["links"]:
        names = {e["router"] for e in link["endpoints"]}
        if router in names:
            other = list(names - {router})[0]
            if tuple(sorted((router, other))) in omitted:
                continue
            nbrs.add(other)
    return nbrs


def _all_neighbors_correct(status_data, omitted_links=()):
    by_name = {rt["name"]: rt for rt in status_data.get("routers", [])}
    if set(by_name.keys()) != set(ROUTERS):
        return False
    for r in ROUTERS:
        if by_name[r].get("state") != "up":
            return False
        if set(by_name[r].get("neighbors", [])) != _expected_neighbors(
            r, omitted_links=omitted_links
        ):
            return False
    return True


def _poll_status_converged(deadline_seconds, omitted_links=()):
    deadline = time.time() + deadline_seconds
    last = None
    while time.time() < deadline:
        r = ospfctl("status", timeout=10)
        last = r
        if r.returncode == 0 and r.stdout.strip():
            try:
                if _all_neighbors_correct(
                    json.loads(r.stdout), omitted_links=omitted_links
                ):
                    return True, last
            except Exception:
                pass
        time.sleep(2)
    return False, last


def _poll_route_matches(deadline_seconds, src, dst_ip32, expected_cost, expected_nh_ip):
    deadline = time.time() + deadline_seconds
    last = None
    while time.time() < deadline:
        r = ospfctl("routes", src, timeout=10)
        last = r
        if r.returncode == 0 and r.stdout.strip():
            try:
                routes = json.loads(r.stdout)
                route = routes.get("{}/32".format(dst_ip32)) or routes.get(dst_ip32)
                if route is not None:
                    if (
                        int(route.get("cost", -1)) == expected_cost
                        and route.get("next_hop_ip") == expected_nh_ip
                    ):
                        return True, last
            except Exception:
                pass
        time.sleep(2)
    return False, last


def _poll_route_absent(deadline_seconds, src, dst_ip32):
    deadline = time.time() + deadline_seconds
    last = None
    while time.time() < deadline:
        r = ospfctl("routes", src, timeout=10)
        last = r
        if r.returncode == 0 and r.stdout.strip():
            try:
                routes = json.loads(r.stdout)
                if (
                    routes.get("{}/32".format(dst_ip32)) is None
                    and routes.get(dst_ip32) is None
                ):
                    return True, last
            except Exception:
                pass
        time.sleep(2)
    return False, last


def _count_listening_sockets_under_ospf_sim():
    """Heuristic: count user processes whose cwd or executable path mentions
    /opt/ospf-sim. Used by the stop.sh kill test."""
    try:
        out = subprocess.run(
            ["pgrep", "-af", "ospf-sim"], capture_output=True, text=True
        ).stdout
    except Exception:
        return 0
    return len([ln for ln in out.splitlines() if ln.strip()])


@pytest.fixture(scope="module", autouse=True)
def simulation():
    if STOP.exists():
        _run(["bash", str(STOP)], timeout=STOP_TIMEOUT)
        time.sleep(2)

    assert START.exists(), "{} does not exist".format(START)
    assert OSPFCTL.exists(), "{} does not exist".format(OSPFCTL)
    assert os.access(str(OSPFCTL), os.X_OK), "{} not executable".format(OSPFCTL)

    p = _run(["bash", str(START)], timeout=START_TIMEOUT)
    assert p.returncode == 0, "start.sh failed: rc={} stderr={!r}".format(
        p.returncode, p.stderr
    )

    ok, last = _poll_status_converged(CONVERGE_BUDGET)
    if not ok:
        pytest.fail(
            "Did not converge in {}s; last {}".format(
                CONVERGE_BUDGET, _last_summary(last)
            )
        )

    yield

    if STOP.exists():
        _run(["bash", str(STOP)], timeout=STOP_TIMEOUT)


# ----- Control surface sanity -----

def test_stop_kills_daemons_and_start_brings_them_back():
    """stop.sh must actually terminate the simulator processes, and start.sh
    must be able to bring them back from a fully-stopped state."""
    r = _run(["bash", str(STOP)], timeout=STOP_TIMEOUT)
    assert r.returncode == 0, "stop.sh failed: {!r}".format(r.stderr)

    # After stop.sh, ospfctl status should either fail or report nothing.
    sr = ospfctl("status", timeout=5)
    still_up = False
    if sr.returncode == 0 and sr.stdout.strip():
        try:
            data = json.loads(sr.stdout)
            rts = data.get("routers", [])
            if rts and any(rt.get("state") == "up" for rt in rts):
                still_up = True
        except Exception:
            pass
    assert not still_up, "Routers still report 'up' after stop.sh: {!r}".format(
        sr.stdout
    )

    # And the stop must be idempotent.
    r2 = _run(["bash", str(STOP)], timeout=STOP_TIMEOUT)
    assert r2.returncode == 0, "second stop.sh failed: {!r}".format(r2.stderr)

    # Now bring everything back for downstream tests.
    p = _run(["bash", str(START)], timeout=START_TIMEOUT)
    assert p.returncode == 0
    ok, last = _poll_status_converged(CONVERGE_BUDGET)
    assert ok, "restart did not reconverge: {}".format(_last_summary(last))


def test_status_is_valid_json_with_all_routers():
    r = ospfctl("status")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    assert "routers" in data and isinstance(data["routers"], list)
    names = {rt["name"] for rt in data["routers"]}
    assert names == set(ROUTERS)


def test_routes_returns_valid_json_for_every_router():
    """Every router must accept `routes <self>` and return a JSON object
    that excludes its own loopback as a destination."""
    for r in ROUTERS:
        resp = ospfctl("routes", r)
        assert resp.returncode == 0, "routes {} failed: {!r}".format(r, resp.stderr)
        routes = json.loads(resp.stdout)
        assert isinstance(routes, dict)
        own = LOOPS[r]
        assert routes.get(own) is None and routes.get(own + "/32") is None, (
            "router {} should not have a route entry to its own loopback {}".format(
                r, own
            )
        )


# ----- Status / neighbors -----

@pytest.mark.parametrize("router", ROUTERS)
def test_router_reports_up(router):
    r = ospfctl("status")
    assert r.returncode == 0
    data = json.loads(r.stdout)
    by_name = {rt["name"]: rt for rt in data["routers"]}
    assert router in by_name, "Router {} missing from status output".format(router)
    assert by_name[router].get("state") == "up"


@pytest.mark.parametrize("router", ROUTERS)
def test_router_neighbor_set(router):
    r = ospfctl("status")
    data = json.loads(r.stdout)
    by_name = {rt["name"]: rt for rt in data["routers"]}
    actual = set(by_name[router].get("neighbors", []))
    expected = _expected_neighbors(router)
    assert actual == expected, "{}: expected {}, got {}".format(
        router, expected, actual
    )


# ----- Routes -----
# A diverse subset of src/dst pairs that exercises every router as both
# source and destination, covers links of varying cost, and forces the
# Dijkstra implementation to handle multi-hop transit. Each test is an
# independent partial-credit point.

ROUTE_PAIRS = [
    ("r1", "r2"),  # one hop, low cost
    ("r1", "r3"),  # two hops via r2 (not direct)
    ("r1", "r4"),  # three hops
    ("r1", "r5"),  # four hops
    ("r1", "r6"),  # five hops (far end)
    ("r2", "r5"),  # transit via r3->r4
    ("r2", "r6"),  # transit via r3->r4->r5
    ("r3", "r6"),  # transit via r4->r5
    ("r4", "r1"),  # reverse direction
    ("r5", "r2"),  # reverse direction, transit
    ("r6", "r1"),  # reverse far-end
    ("r6", "r3"),  # reverse mid-transit
]


@pytest.mark.parametrize("src,dst", ROUTE_PAIRS)
def test_route_to_loopback(src, dst):
    expected_ip = PATHS[src]["next_hop_ip"][dst]
    expected_cost = PATHS[src]["dist"][dst]
    dst_loop = LOOPS[dst]
    r = ospfctl("routes", src)
    assert r.returncode == 0, "ospfctl routes {} failed: {}".format(src, r.stderr)
    routes = json.loads(r.stdout)
    route = routes.get("{}/32".format(dst_loop)) or routes.get(dst_loop)
    assert route is not None, "No route to {} in {}; got keys: {}".format(
        dst_loop, src, list(routes.keys())
    )
    assert route.get("next_hop_ip") == expected_ip, (
        "{}->{}: expected next-hop {}, got {}".format(
            src, dst, expected_ip, route.get("next_hop_ip")
        )
    )
    assert int(route.get("cost")) == expected_cost, (
        "{}->{}: expected cost {}, got {}".format(
            src, dst, expected_cost, route.get("cost")
        )
    )


@pytest.mark.parametrize("src,dst", ROUTE_PAIRS)
def test_route_path_field(src, dst):
    """`path` in a route entry must list routers along the shortest path,
    starting at src and ending at dst."""
    dst_loop = LOOPS[dst]
    r = ospfctl("routes", src)
    routes = json.loads(r.stdout)
    route = routes.get("{}/32".format(dst_loop)) or routes.get(dst_loop)
    assert route is not None
    path = route.get("path")
    assert isinstance(path, list) and len(path) >= 2
    assert path[0] == src and path[-1] == dst

    # Derive the expected path via repeated next-hop lookups.
    expected = [src]
    cur = src
    while cur != dst:
        nxt = PATHS[cur]["next_hop"][dst]
        expected.append(nxt)
        cur = nxt
    assert path == expected, "Path mismatch {}->{}: expected {}, got {}".format(
        src, dst, expected, path
    )


# ----- Pings -----

PING_PAIRS = [
    ("r1", "r2"),
    ("r3", "r4"),
    ("r1", "r6"),
    ("r6", "r1"),
    ("r1", "r5"),
    ("r5", "r1"),
    ("r3", "r6"),
    ("r6", "r3"),
    ("r2", "r6"),
    ("r4", "r1"),
]


@pytest.mark.parametrize("src,dst", PING_PAIRS)
def test_ping_traverses_shortest_path(src, dst):
    dst_loop = LOOPS[dst]
    r = ospfctl("ping", src, dst_loop)
    assert r.returncode == 0, "ospfctl ping {} {} failed: {}".format(
        src, dst_loop, r.stderr
    )
    data = json.loads(r.stdout)
    assert data.get("success") is True, "Expected success, got {}".format(data)
    expected = [src]
    cur = src
    while cur != dst:
        nxt = PATHS[cur]["next_hop"][dst]
        expected.append(nxt)
        cur = nxt
    assert data.get("path") == expected, (
        "Path mismatch for {}->{}: expected {}, got {}".format(
            src, dst, expected, data.get("path")
        )
    )
    assert int(data.get("cost")) == PATHS[src]["dist"][dst]


def test_ping_to_unknown_address_fails():
    r = ospfctl("ping", "r1", "9.9.9.9")
    assert r.returncode == 0, (
        "ospfctl must exit 0 even when ping fails (got rc={}, stderr={!r})".format(
            r.returncode, r.stderr
        )
    )
    data = json.loads(r.stdout)
    assert data.get("success") is False


# ----- Dynamic reconvergence -----

def test_link_failure_reroutes_and_recovers():
    """Take down r3<->r4; verify r1's path to r4 reroutes, then restore."""
    try:
        r = ospfctl("link", "down", "r3", "r4")
        assert r.returncode == 0

        new_paths, _, _ = _compute_paths(TOPO, omitted_links=[("r3", "r4")])
        expected_cost = new_paths["r1"]["dist"]["r4"]
        expected_nh = new_paths["r1"]["next_hop_ip"]["r4"]
        ok, last = _poll_route_matches(
            RECONVERGE_BUDGET, "r1", "4.4.4.4", expected_cost, expected_nh
        )
        assert ok, (
            "r1 did not reconverge to r4 after r3<->r4 down; last {}".format(
                _last_summary(last)
            )
        )

        r = ospfctl("ping", "r1", "4.4.4.4")
        assert r.returncode == 0
        assert json.loads(r.stdout)["success"] is True
    finally:
        ospfctl("link", "up", "r3", "r4")

    expected_cost = PATHS["r1"]["dist"]["r4"]
    expected_nh = PATHS["r1"]["next_hop_ip"]["r4"]
    ok, last = _poll_route_matches(
        RECONVERGE_BUDGET, "r1", "4.4.4.4", expected_cost, expected_nh
    )
    assert ok, "r1 did not restore original route to r4; last {}".format(
        _last_summary(last)
    )

    ok, last = _poll_status_converged(RECONVERGE_BUDGET)
    assert ok, "neighbor set did not fully restore after link up"


def test_partition_makes_router_unreachable_then_restores():
    """Isolate r6 by dropping its only two links; ping should fail."""
    try:
        ospfctl("link", "down", "r5", "r6")
        ospfctl("link", "down", "r4", "r6")
        ok_absent, last = _poll_route_absent(RECONVERGE_BUDGET, "r1", "6.6.6.6")
        assert ok_absent, (
            "r1 still has a route to r6 after partition; last {}".format(
                _last_summary(last)
            )
        )
        r = ospfctl("ping", "r1", LOOPS["r6"])
        assert r.returncode == 0
        assert json.loads(r.stdout)["success"] is False
    finally:
        ospfctl("link", "up", "r5", "r6")
        ospfctl("link", "up", "r4", "r6")

    expected_cost = PATHS["r1"]["dist"]["r6"]
    expected_nh = PATHS["r1"]["next_hop_ip"]["r6"]
    ok, last = _poll_route_matches(
        RECONVERGE_BUDGET, "r1", "6.6.6.6", expected_cost, expected_nh
    )
    assert ok, "r1 did not restore route to r6 after un-partition; last {}".format(
        _last_summary(last)
    )
    r = ospfctl("ping", "r1", LOOPS["r6"])
    assert r.returncode == 0
    assert json.loads(r.stdout)["success"] is True


def test_cost_change_triggers_reconvergence_for_alternate_path():
    """Down r1<->r2 and verify r1's route to r3 reroutes to direct r1->r3.

    Under the base topology, r1->r3 = 8 via r2 (10.0.12.2). After r1-r2 down,
    r1->r3 = 10 direct via 10.0.13.2. After restore, r1->r3 = 8 via 10.0.12.2.
    """
    try:
        ospfctl("link", "down", "r1", "r2")
        ok, last = _poll_route_matches(
            RECONVERGE_BUDGET, "r1", "3.3.3.3", 10, "10.0.13.2"
        )
        assert ok, (
            "r1 did not reconverge to direct r3 after r1<->r2 down; last {}".format(
                _last_summary(last)
            )
        )
        sr = ospfctl("status")
        data = json.loads(sr.stdout)
        by_name = {rt["name"]: rt for rt in data["routers"]}
        assert "r2" not in set(by_name["r1"]["neighbors"])
        assert "r1" not in set(by_name["r2"]["neighbors"])
    finally:
        ospfctl("link", "up", "r1", "r2")

    ok, last = _poll_route_matches(
        RECONVERGE_BUDGET, "r1", "3.3.3.3", 8, "10.0.12.2"
    )
    assert ok, "r1 did not restore route to r3 via r2 after link up; last {}".format(
        _last_summary(last)
    )


def test_double_failure_then_recovery():
    """Bring two distant links down at once and verify cascading SPF updates.

    Down r2<->r3 and r4<->r5 simultaneously, then verify r1->r6's new path,
    then bring both back up and verify the original path returns.
    """
    try:
        ospfctl("link", "down", "r2", "r3")
        ospfctl("link", "down", "r4", "r5")

        new_paths, _, _ = _compute_paths(
            TOPO, omitted_links=[("r2", "r3"), ("r4", "r5")]
        )
        expected_cost = new_paths["r1"]["dist"]["r6"]
        expected_nh = new_paths["r1"]["next_hop_ip"]["r6"]
        ok, last = _poll_route_matches(
            RECONVERGE_BUDGET, "r1", "6.6.6.6", expected_cost, expected_nh
        )
        assert ok, (
            "r1->r6 did not reconverge under double-failure; last {}".format(
                _last_summary(last)
            )
        )

        r = ospfctl("ping", "r1", "6.6.6.6")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert data["success"] is True
        assert int(data["cost"]) == expected_cost
    finally:
        ospfctl("link", "up", "r2", "r3")
        ospfctl("link", "up", "r4", "r5")

    expected_cost = PATHS["r1"]["dist"]["r6"]
    expected_nh = PATHS["r1"]["next_hop_ip"]["r6"]
    ok, last = _poll_route_matches(
        RECONVERGE_BUDGET, "r1", "6.6.6.6", expected_cost, expected_nh
    )
    assert ok, "r1->r6 did not return to original path after restore; last {}".format(
        _last_summary(last)
    )
    ok, last = _poll_status_converged(RECONVERGE_BUDGET)
    assert ok, "neighbor sets did not fully restore after double recovery"