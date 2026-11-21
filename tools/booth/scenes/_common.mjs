// Shared by the scenes: facts read from the running cluster, and small wording helpers.

export const money = (n) => `$${Number(n).toLocaleString('en-US')}`;
export const list = (items) => (items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`);
/** "5 s" (how the stage reports a duration) -> "5 seconds". */
export const seconds = (text) => { const n = Number(String(text).replace(/[^\d.]/g, '')); return `${n} second${n === 1 ? '' : 's'}`; };

/** Facts every scene starts from. Captions use them as {nodes}, {regions}, {cloud}, {places}, {primary}, {primaryPlace}. */
export async function clusterFacts(d) {
  const s = await d.state();
  const aws = s.regions.every((r) => r.provider === 'aws');
  const primary = s.regions.find((r) => r.primary) || s.regions[0];
  d.fact('nodes', s.cluster.total_nodes);
  d.fact('regions', s.regions.length);
  d.fact('cloud', aws ? 'AWS' : 'cloud');
  d.fact('places', list(s.regions.map((r) => r.label)));
  d.fact('primary', primary.name);
  d.fact('primaryPlace', primary.label);
  d.fact('primaryNodes', primary.nodes.length);
  d.fact('primaryZones', primary.zones.length);
  d.fact('cluster', `${s.cluster.total_nodes}-node CockroachDB cluster · ${s.regions.length} ${aws ? 'AWS' : 'cloud'} regions`);
  return s;
}

/** Start a guided scenario and wait until the stage offers its first step. */
export async function startScenario(d, id) {
  const r = await d.api('POST', `/api/scenarios/${id}/start`);
  if (!r.ok) throw new Error(`could not start scenario ${id}: ${JSON.stringify(r.data)}`);
  const deadline = Date.now() + 300000;
  for (;;) {
    const cur = await d.scenario();
    if (cur && !cur.busy) return cur;
    if (Date.now() > deadline) throw new Error(`scenario ${id} did not finish preparing`);
    await d.settle(0.5);
  }
}
