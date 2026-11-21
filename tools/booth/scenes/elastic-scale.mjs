// Elastic scale: add a node and a whole region to a live cluster under load, then drain and remove them. Payments
// run through all of it: the new region takes its share of the customers, and gives them back before it is retired.
import { clusterFacts, list, money, startScenario } from './_common.mjs';

export default {
  id: 'elastic-scale',
  title: 'Elastic scale',
  premise: 'Add a node and a whole region to a live cluster, then take them away.',

  async prepare(d) {
    await clusterFacts(d);
    const started = await d.api('POST', '/api/workload/start');
    if (!started.ok) throw new Error(`Payments did not start: ${JSON.stringify(started.data)}`);
    const flowing = await d.until((s) => s.workload.running && s.workload.tps > 150 && s.workload.invariant_ok && s, { timeout: 120, what: 'payments to flow' });
    d.fact('tps', Math.round(flowing.workload.tps / 50) * 50);
    d.fact('total', money(flowing.workload.expected));
    const scenario = await startScenario(d, 'elastic-scale');
    d.fact('newRegion', scenario.steps[1].label.replace(/^Add region /, ''));
    const catalog = (await d.api('GET', '/api/catalog')).data || {};   // the place name, known before the region exists
    const known = Object.values(catalog).flat().find((r) => r && r.name === d.facts.newRegion);
    d.fact('newPlaceAhead', known && known.label ? known.label : d.facts.newRegion);
    await d.settle(20);   // let the chart fill with steady traffic before the camera rolls
  },

  async run(d) {
    const f = d.facts;
    // Every second of Payments through the whole take, from the app's own per-second history: how many payments
    // committed in it. "Never stopped" is claimed only if no second of the take had none; a stopped workload has
    // no seconds at all, which the check at the end also catches.
    const seconds = new Map(); let stopped = false;
    const from = ((await d.state()).workload.history.at(-1) || { ts: 0 }).ts;
    const sample = async () => {
      try {
        const w = (await d.state()).workload;
        if (!w.running) stopped = true;
        for (const h of w.history) if (h.ts > from) seconds.set(h.ts, h.commits);
      } catch { /* next sample */ }
    };
    const watch = setInterval(sample, 2000);
    // Stop a take as soon as it can no longer show what it is meant to: a stall costs minutes then, not the whole take.
    const steady = async (when) => {
      await sample();
      const without = [...seconds].filter(([, commits]) => commits === 0).map(([ts]) => ts - from);
      if (stopped || without.length) throw new Error(`payments stalled ${when} (no payment in second ${without.join(', ')} of the take): this take cannot show that they never stop`);
    };
    try {
      await d.say('This is a live CockroachDB cluster: {nodes} nodes across {regions} {cloud} regions.');
      await d.shot('kpi:tps+kpi:p99+kpi:money+kpi:recovery');
      await d.say('It is taking about {tps} payments a second. They keep running while the cluster changes.');

      await d.shot('wide');
      await d.say(f.primaryNodes === 3 ? 'First, more capacity where the load is: a fourth node in {primaryPlace}.'
        : 'First, more capacity where the load is: one more node in {primaryPlace}.');
      const addNode = await d.next();                                 // Add a node in the primary
      await d.shot(`region:${f.primary}`);
      await d.say('A new node starts and joins. Nobody tells it what data to hold.', { wait: false });
      const added = (await addNode.done()).result;
      d.fact('newNode', added['New node']);
      const holding = await d.until((s) => { const n = s.nodes.find((x) => x.name === f.newNode); return n && n.liveness === 'live' && n.replicas >= 8 && n; },
        { timeout: 240, lapse: true, what: 'data to move onto the new node' });
      d.fact('newNodeRanges', holding.replicas);
      await d.say('The cluster is already moving data onto it: {newNodeRanges} ranges (chunks of data) so far.');

      await d.shot('wide');
      await d.say('Now something bigger: a whole new region, {newPlaceAhead}, with three nodes.');
      const addRegion = await d.next();                               // Add the region
      await d.say('One statement per database: ALTER DATABASE ... ADD REGION.', { wait: false });
      const joined = (await addRegion.done({ timeout: 1200 })).result;
      d.check(joined.Payments === 'now serve customers there', 'payments serve customers in the new region');
      const grown = await d.until((s) => s.regions.some((r) => r.name === f.newRegion) && s.cluster.status === 'healthy' && s, { timeout: 300, lapse: true, what: 'the new region to be healthy' });
      d.fact('nodesNow', grown.cluster.total_nodes);
      d.fact('regionsNow', grown.regions.length);
      d.fact('newPlace', grown.regions.find((r) => r.name === f.newRegion).label);
      d.fact('placesNow', list(grown.regions.map((r) => r.label)));
      await d.say('{nodesNow} nodes in {regionsNow} regions: {placesNow}.');
      await d.shot(`region:${f.newRegion}`);
      await d.until((s) => s.nodes.filter((n) => n.region === f.newRegion).every((n) => n.liveness === 'live' && n.replicas > 0),
        { timeout: 240, lapse: true, what: 'data to reach the new region' });
      await d.say('{newPlace} holds data now, and a share of the customers: their accounts moved there.');
      // Its payments run locally once the region's leases are home; until then every statement crosses an ocean.
      await d.shot('chart');
      const share = await d.until((s) => { const home = s.workload.by_home[f.newRegion]; return home && home.tps >= 45 && home; },
        { timeout: 900, lapse: true, what: 'the new region to take its share of the payments' });
      d.fact('newShare', Math.floor(share.tps / 10) * 10);
      await d.say('Payments by region: the new band is {newPlace}, about {newShare} payments a second of its own.');

      await d.shot('kpi:tps+kpi:p99+kpi:money+kpi:recovery');
      const mid = await d.state();
      d.check(mid.workload.money_lost === 0 && mid.workload.invariant_ok, 'no money was lost while the cluster grew');
      await steady('while the cluster grew');
      await d.say('No maintenance window. Payments kept flowing, and the Money lost counter still reads $0.');

      await d.shot('wide');
      await d.say('Scaling back in is just as calm. First the extra node: it drains its data, then leaves.');
      const drain = await d.next();                                   // Decommission the new node
      const drained = (await drain.done({ timeout: 2700 })).result;
      const drainMinutes = Math.max(1, Math.round(parseFloat(drained.Took) / 60));
      d.fact('drainTook', `${drainMinutes} minute${drainMinutes === 1 ? '' : 's'}`);
      await steady('while the extra node drained');
      await d.say('The node is gone. Its data moved to the others first: about {drainTook}, sped up here.');

      await d.say('Last, {newPlace} is retired. Its customers move to the other regions first, while payments run.');
      const retire = await d.next();                                  // Remove the region
      const retired = (await retire.done({ timeout: 2700 })).result;
      d.check(retired.Payments === 'kept running: its customers moved first', 'payments kept running while the region was retired');
      const back = await d.until((s) => s.cluster.total_nodes === f.nodes && s.regions.length === f.regions && s.cluster.status === 'healthy' && s,
        { timeout: 600, lapse: true, what: 'the cluster to be back to its starting shape' });
      d.check(back.workload.money_lost === 0 && back.workload.invariant_ok, 'no money was lost while the cluster shrank');
      await sample();
      const counts = [...seconds.values()];
      const slowest = counts.length ? Math.min(...counts) : 0;
      if (slowest === 0) d.fact('stalledAt', [...seconds].filter(([, commits]) => commits === 0).map(([ts]) => ts - from).join(', '));
      d.check(!stopped && counts.length > 60 && slowest > 0, 'payments never stopped: every second of the take saw payments commit');
      d.fact('slowest', Math.floor(slowest / 10) * 10);
      await d.say('Back to {nodes} nodes in {regions} regions.');
      await d.shot('kpi:tps+kpi:p99+kpi:money+kpi:recovery');
      await d.say(f.slowest >= 20 ? 'Payments never stopped: the slowest second still saw about {slowest} payments. $0 lost.'
        : 'Payments never stopped, and not one dollar was lost.');
      await d.shot('wide');
    } finally {
      clearInterval(watch);
    }
  },

  outro: (f) => ['A node and a whole region added to a live cluster', 'Then drained and removed, while payments kept running', `$0 lost of ${f.total}`],
};
