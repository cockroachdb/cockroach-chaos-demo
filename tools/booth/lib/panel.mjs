// The control plane's API, for the booth recorder: read state, wait for health, and put the cluster back the way a
// take found it.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function panel(base = 'http://localhost:8088') {
  const api = async (method, path, body) => {
    const r = await fetch(base + path, { method, headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(180000) });
    const text = await r.text();
    let data = text;
    try { data = JSON.parse(text); } catch { /* not JSON */ }
    return { status: r.status, ok: r.ok, data };
  };
  const state = async () => (await api('GET', '/api/state')).data;
  const scenario = async () => (await api('GET', '/api/scenarios/current')).data;

  const waitFor = async (fn, timeoutS, what) => {
    const end = Date.now() + timeoutS * 1000;
    let last;
    for (;;) {
      try { const v = await fn(); if (v) return v; } catch (e) { last = e; }
      if (Date.now() > end) throw new Error(`timed out after ${timeoutS} s waiting for ${what}${last ? ` (${last.message})` : ''}`);
      await sleep(1000);
    }
  };
  const idle = (timeoutS = 900) => waitFor(async () => { const op = (await state()).operation; return !op || op.status !== 'running'; }, timeoutS, 'the cluster operation to finish');
  const healthy = (nodes, timeoutS = 300) => waitFor(async () => {
    const s = await state(); const c = s.cluster || {};
    return c.status === 'healthy' && c.live_nodes === c.total_nodes && (!nodes || c.total_nodes === nodes) ? s : null;
  }, timeoutS, `a healthy cluster${nodes ? ` with ${nodes} nodes` : ''}`);

  /** What a take must put back: who is in the cluster, whether Payments ran, the survival goal. */
  const snapshot = async () => {
    const s = await state();
    return { nodes: s.nodes.map((n) => n.name), regions: s.regions.map((r) => r.name), payments: Boolean(s.workload && s.workload.running),
      survival: s.cluster.survival_goal, primary: (s.regions.find((r) => r.primary) || {}).name };
  };

  /** Stop the scenario, recover everything, remove what the take added, stop Payments if the take started it. */
  const restore = async (before, log = () => {}) => {
    if (await scenario().catch(() => null)) { await api('POST', '/api/scenarios/stop'); await sleep(2000); log('scenario stopped'); }
    await idle().catch((e) => log(String(e.message)));
    await api('POST', '/api/chaos', { action: 'recover', kind: 'all', value: '' });
    let s = await state();
    if (!before.payments && s.workload && s.workload.running) { await api('POST', '/api/workload/stop'); log('Payments stopped'); }
    for (const r of s.regions.map((x) => x.name).filter((x) => !before.regions.includes(x))) {
      await idle(); log(`removing region ${r}`);
      await api('DELETE', `/api/cluster/regions/${encodeURIComponent(r)}`); await sleep(1500); await idle(1500);
    }
    s = await state();
    for (const n of s.nodes.map((x) => x.name).filter((x) => !before.nodes.includes(x))) {
      await idle(); log(`removing node ${n}`);
      await api('DELETE', `/api/cluster/nodes/${encodeURIComponent(n)}`); await sleep(1500); await idle(1500);
    }
    s = await state();
    if (s.cluster.survival_goal !== before.survival) {
      log(`survival goal back to ${before.survival}`);
      await api('POST', '/api/cluster/survival', { goal: before.survival }); await sleep(1500); await idle();
    }
    const primary = (s.regions.find((r) => r.primary) || {}).name;
    if (before.primary && primary !== before.primary) {
      log(`primary region back to ${before.primary}`);
      await api('POST', '/api/cluster/primary', { region: before.primary }); await sleep(1500); await idle();
    }
    await healthy(before.nodes.length, 600);
    log(`cluster healthy, ${before.nodes.length} nodes`);
  };

  return { base, api, state, scenario, waitFor, idle, healthy, snapshot, restore };
}
