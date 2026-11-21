// ZONE vs REGION survival: the same region outage twice. Under ZONE survival the lost region's customers stall;
// after one ALTER DATABASE (run live) the same outage is survived.
import { clusterFacts, money, seconds, startScenario } from './_common.mjs';

const NUMBERS = 'kpi:tps+kpi:p99+kpi:money+kpi:recovery';

export default {
  id: 'zone-vs-region',
  title: 'ZONE vs REGION survival',
  premise: 'The same region outage, before and after one statement.',

  async prepare(d) {
    const s = await clusterFacts(d);
    d.fact('others', s.regions.filter((r) => !r.primary).map((r) => r.label).join(' and '));
    await startScenario(d, 'zone-vs-region');
  },

  async run(d) {
    const f = d.facts;
    await d.say('A live CockroachDB cluster: {nodes} nodes across {regions} {cloud} regions.');
    await d.say('How much failure a database survives is a setting. Here it is changed live, twice.');

    const zone = await d.next();                                     // Switch to ZONE survival
    await d.say("First, ZONE survival: each region keeps all three copies of its customers' data at home.", { wait: false });
    await zone.done({ timeout: 900 });
    const zoned = await d.until((s) => s.cluster.survival_goal === 'zone' && s.workload.running && s.workload.tps > 50 && s, { timeout: 120, what: 'payments under ZONE survival' });
    d.fact('total', money(zoned.workload.expected));
    await d.shot('#chip-goal', { pad: 150 });
    await d.hover('#chip-goal');
    await d.say('Local and fast. It survives one failed data center, but losing a whole region takes its data offline.');
    await d.unhover();

    await d.shot('wide');
    await d.say('Payments are running in every region. Cut the power to all of {primaryPlace}.');
    const first = await d.next();                                    // Take down the primary
    await d.shot(`region:${f.primary}`);
    const stalled = (await first.done({ lapse: false })).result;
    d.check(stalled[`Payments homed in ${f.primary}`] === 'stalled until the region returns', "the lost region's payments stalled under ZONE survival");
    d.check(String(stalled['Other regions']).startsWith('keep paying'), 'the other regions kept paying under ZONE survival');
    d.fact('othersRate', stalled['Other regions'].replace('keep paying: ', '').replace('/s', ' a second'));
    await d.say('{primaryPlace} is down, and payments for its customers stop: every copy of their data was there.');
    await d.shot('scenario');
    await d.say('{others} keep serving their own customers: {othersRate}.');

    await d.shot('wide');
    const recover = await d.next();                                  // Recover the primary
    await d.say('When the region returns, its customers can pay again.', { wait: false });
    await recover.done();
    await d.until((s) => s.cluster.status === 'healthy' && s.cluster.live_nodes === s.cluster.total_nodes, { timeout: 240, lapse: true, what: 'the region to come back' });

    await d.say('Now one statement: ALTER DATABASE ... SURVIVE REGION FAILURE.');
    const region = await d.next();                                   // Switch to REGION survival
    await d.say('The database spreads copies of every row across all {regions} regions, while payments run.', { wait: false });
    const spread = (await region.done({ timeout: 1200 })).result;
    d.fact('rebalanced', seconds(spread['Rebalanced in']));
    await d.shot('#chip-goal', { pad: 150 });
    await d.say('Done in {rebalanced}. No restart needed.');

    await d.shot('wide');
    await d.say('The same failure again: all of {primaryPlace} loses power.');
    const second = await d.next();                                   // Take down the primary again
    await d.shot(`region:${f.primary}`);
    const survived = (await second.done({ lapse: false })).result;
    d.check(/^\d+ s$/.test(survived['Recovery time']) && survived['Money lost'] === '$0', 'under REGION survival payments recovered and no money was lost');
    d.fact('recovery', seconds(survived['Recovery time']));
    await d.say('{primaryPlace} is down again. This time its customers are served from the other regions.');
    await d.shot(NUMBERS);
    await d.say('Payments recovered in about {recovery}. Money lost: $0.');

    await d.shot('wide');
    const back = await d.next();                                     // Recover
    await d.say('Power returns, and the region rejoins.', { wait: false });
    await back.done({ lapse: false });
    const whole = await d.until((s) => s.cluster.status === 'healthy' && s.cluster.live_nodes === s.cluster.total_nodes && s,
      { timeout: 240, lapse: true, what: 'the region to come back' });
    d.check(whole.workload.money_lost === 0 && whole.workload.invariant_ok, 'the accounts still add up');
    await d.pause(2.5);
    await d.say('Same hardware, same outage. One setting decided whether customers could keep paying.');
  },

  outro: (f) => ["ZONE survival: the outage stalled the lost region's customers", `REGION survival: the same outage, recovered in about ${f.recovery}`,
    `Switched live in ${f.rebalanced}, with one statement`],
};
