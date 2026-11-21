// The two "lose it under load" videos share one script: a whole region, or one availability zone, loses power while
// payments flow; payments recover and no money is lost. Every number in a caption is measured in the take.
import { clusterFacts, money, seconds, startScenario } from './_common.mjs';

const NUMBERS = 'kpi:tps+kpi:p99+kpi:money+kpi:recovery';

/** kind: 'region' (the primary region goes down) or 'zone' (one availability zone of a region goes down). */
export function survive({ id, title, premise, kind }) {
  const region = kind === 'region';
  return {
    id, title, premise,

    async prepare(d) {
      const s = await clusterFacts(d);
      const scenario = await startScenario(d, id);
      if (!region) {
        const zone = scenario.steps[1].label.replace(/^Take down /, '');     // the scenario picks the zone
        const home = s.regions.find((r) => r.zones.includes(zone));
        d.fact('zone', zone);
        d.fact('home', home.name);
        d.fact('homePlace', home.label);
        d.fact('zoneNodes', s.nodes.filter((n) => n.zone === zone).length);
      } else {
        d.fact('home', d.facts.primary);
        d.fact('homePlace', d.facts.primaryPlace);
      }
    },

    async run(d) {
      const f = d.facts;
      await d.say('This is a live CockroachDB cluster: {nodes} nodes across {regions} {cloud} regions.');
      if (region) {
        d.check((await d.state()).cluster.survival_goal === 'region', 'the database is set to survive a region failure');
        await d.shot('#chip-goal', { pad: 150 });
        await d.hover('#chip-goal');
        await d.say('It is set to survive losing a whole region: every row is stored in all {regions}.');
        await d.unhover();
        await d.shot(`region:${f.home}`);
        await d.say('Each region has {primaryNodes} nodes in {primaryZones} availability zones: separate data centers. This is {primaryPlace}.');
      } else {
        await d.shot(`region:${f.home}`);
        await d.say('Each region spreads its nodes over {primaryZones} availability zones: separate data centers.');
      }
      await d.shot('wide');
      await d.say('It runs a payments system. Time to start moving money.');

      const start = await d.next();                                   // Start payments
      await d.say('Every payment is one transaction: debit one account, credit another.', { wait: false });
      await start.done();
      const flowing = await d.until((s) => s.workload.running && s.workload.tps > 50 && s.workload.invariant_ok && s, { what: 'payments to flow' });
      d.fact('tps', Math.round(flowing.workload.tps / 50) * 50);      // "about": the number on screen moves by a few each second
      d.fact('total', money(flowing.workload.expected));
      d.check(flowing.workload.money_lost === 0, 'the accounts add up before the failure');
      d.check(flowing.workload.homing.regions.length === f.regions, 'payments come from customers in every region');
      await d.shot(NUMBERS);
      await d.say('About {tps} payments a second, from customers in all {regions} regions.');
      await d.say('The accounts hold exactly {total}. If a failure loses one dollar, Money lost shows it.');

      await d.shot('wide');
      await d.say(region ? 'Now the worst case: cut the power to all of {primaryPlace}, the primary region.'
        : 'Now a data center fails: availability zone {zone} in {homePlace} loses power.');
      const failedBefore = (await d.state()).workload.failed_total;
      const outage = await d.next();                                  // Take it down
      await outage.done({ lapse: false });
      await d.shot(`region:${f.home}`);
      await d.say('No warning, no graceful shutdown. The cluster notices within seconds.', { wait: false });
      if (region) {
        await d.until((s) => s.regions.find((r) => r.name === f.home).state === 'down', { timeout: 60, what: 'the region to show as down' });
        await d.say('{primaryNodes} nodes down. The whole region is gone.');
        const moved = await d.until((s) => s.workload.homing.failover[f.home] && s, { timeout: 60, what: 'payments to fail over' });
        d.fact('failover', moved.regions.find((r) => r.name === moved.workload.homing.failover[f.home]).label);
        await d.shot('chart');
        await d.say('Payments per second, by region. The purple band, {primaryPlace}, drops to zero.');
        await d.say('Its customers are now served from {failover}. The total dips, then climbs back.');
      } else {
        const hit = await d.until((s) => s.nodes.filter((n) => n.zone === f.zone).every((n) => n.liveness === 'dead') && s,
          { timeout: 60, what: 'the zone to show as down' });
        d.check(hit.regions.find((r) => r.name === f.home).state === 'degraded', 'the rest of the region is still up');
        await d.say(f.zoneNodes === 1 ? 'The node in {zone} is down. The other zones are untouched.' : 'The nodes in {zone} are down. The other zones are untouched.');
        d.check(!hit.workload.homing.failover[f.home], 'the region still serves its own customers');
        await d.say('{homePlace} keeps serving its own customers from the zones it has left.');
      }

      await d.shot('wide');
      const measure = await d.next();                                 // Measure the impact
      const impact = (await measure.done()).result;
      d.check(/^\d+ s$/.test(impact['Recovery time']), `payments recovered and the stage measured how long it took (it says "${impact['Recovery time']}")`);
      d.check(impact['Money lost'] === '$0', 'no money was lost');
      d.fact('dipped', impact['Recovery time'] !== '0 s');
      d.fact('recovery', seconds(impact['Recovery time']));
      d.fact('lost', impact['Money lost']);
      d.fact('retried', impact['Transfers retried']);
      d.fact('failed', (await d.state()).workload.failed_total - failedBefore);
      await d.shot('scenario');
      await d.say(f.dipped ? 'Payments recovered in about {recovery}.' : 'Throughput never dipped.');
      if (f.retried > 0) {
        await d.say(f.failed === 0 ? '{retried} payments hit the outage and were retried automatically. None failed.'
          : '{retried} payments hit the outage and were retried. {failed} could not complete.');
      }

      await d.shot(NUMBERS);
      await d.say('Money lost: {lost}. Every dollar of the {total} is still there.');
      const now = await d.state();
      d.fact('live', now.cluster.live_nodes);
      await d.shot('kpi:nodes+kpi:unavail');
      if (now.ranges.unavailable === 0) await d.say('{live} of {nodes} nodes are left, and all the data is still reachable.');

      await d.shot('wide');
      await d.say('No failover script and no operator. The database did this by itself.');
      await d.say('Now the power comes back.');
      const back = await d.next();                                    // Bring it back
      // Stay wide: the nodes are back within seconds, and the stage celebrates across its middle.
      await d.say(region ? 'The nodes restart, rejoin, and catch up on what they missed.' : 'The node restarts, rejoins, and catches up on what it missed.', { wait: false });
      await back.done({ lapse: false });
      const whole = await d.until((s) => s.cluster.status === 'healthy' && s.cluster.live_nodes === s.cluster.total_nodes && s,
        { timeout: 240, lapse: true, what: 'everything to come back' });
      d.check(whole.workload.money_lost === 0 && whole.workload.invariant_ok, 'the accounts still add up after the recovery');
      await d.pause(2.5);
      await d.say('All {nodes} nodes are serving again. {lost} lost.');
    },

    outro: (f) => [
      region ? `A whole region lost at ${f.tps} payments a second` : `An availability zone lost at ${f.tps} payments a second`,
      f.dipped ? `Payments recovered in about ${f.recovery}` : 'Throughput never dipped',
      `${f.lost} lost of ${f.total}`,
    ],
  };
}
