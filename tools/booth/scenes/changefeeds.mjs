// Changefeeds that survive failure: every committed change streams out as an event; the node running the stream is
// crashed and another node resumes it from its last checkpoint.
import { clusterFacts, money, startScenario } from './_common.mjs';

export default {
  id: 'changefeeds',
  title: 'Changefeeds that survive failure',
  premise: 'Every change streams out as an event, even when the node running the stream dies.',

  async prepare(d) {
    await clusterFacts(d);
    await startScenario(d, 'changefeeds');
  },

  async run(d) {
    const f = d.facts;
    await d.say('A live payments system: {nodes} nodes across {regions} {cloud} regions.');
    await d.say('Other systems need to hear about every change: analytics, search, fraud checks.');
    const create = await d.next();                                   // Create the changefeed
    await d.say('A changefeed streams each committed change to them as an event.', { wait: false });
    const feed = (await create.done()).result;
    d.check(feed['Events received'] > 0, 'events are arriving at the receiver');
    d.fact('events', Number(feed['Events received']).toLocaleString('en-US'));
    d.fact('rate', Math.round(feed['Events/s'] / 10) * 10);
    const flowing = await d.state();
    d.fact('total', money(flowing.workload.expected));
    await d.shot('scenario');
    await d.say(f.rate >= 10 ? 'The feed is live: {events} events delivered so far, about {rate} a second.' : 'The feed is live: {events} events delivered so far.');

    await d.say('One node coordinates the feed. We crash that exact node.');
    const crash = (await d.step({ lapse: false })).result;           // Crash the feed's node
    d.fact('victim', crash.Crashed);
    const hit = await d.state();
    const home = hit.regions.find((r) => r.nodes.includes(f.victim));
    d.fact('victimPlace', home.label);
    await d.shot(`region:${home.name}`);
    await d.say('The node running the feed, in {victimPlace}, is gone.', { wait: false });
    await d.until((s) => s.nodes.find((n) => n.name === f.victim).liveness === 'dead', { timeout: 60, what: 'the node to show as down' });

    await d.shot('wide');
    const resume = await d.next();                                   // Watch it resume
    await d.say('The changefeed is a job that belongs to the cluster, not to one node.', { wait: false });
    const resumed = (await resume.done({ timeout: 300 })).result;
    const paused = parseFloat(resumed['Longest pause in the stream']);
    d.check(Number.isFinite(paused) && resumed['Events received'] > feed['Events received'], 'events kept arriving after the crash and the pause was measured');
    d.fact('pause', `${Math.round(paused)} second${Math.round(paused) === 1 ? '' : 's'}`);
    await d.shot('scenario');
    await d.say('Another node picked it up from its last checkpoint.');
    await d.say(paused >= 2 ? 'The stream stood still for {pause}, then carried on.' : 'The stream never stood still for more than a second or two.');
    await d.say('Delivery is at least once: after a failure an event may repeat, but none is skipped.');

    await d.shot('wide');
    const back = await d.next();                                     // Recover the node
    await d.say('The crashed node restarts and rejoins.', { wait: false });
    await back.done({ lapse: false });
    const whole = await d.until((s) => s.cluster.status === 'healthy' && s.cluster.live_nodes === s.cluster.total_nodes && s,
      { timeout: 240, lapse: true, what: 'the node to come back' });
    d.check(whole.workload.money_lost === 0 && whole.workload.invariant_ok, 'the accounts still add up');
    await d.pause(2.5);
    await d.say('The crashed node is back. The feed carried on, and no money was lost.');
  },

  outro: (f) => ['The node running the changefeed was crashed', `Another node resumed it: the stream stood still for ${f.pause}`, 'At-least-once delivery: nothing skipped'],
};
