// Follower reads: a read answered by the nearest copy, a few seconds stale, instead of by a leaseholder far away.
import { clusterFacts, startScenario } from './_common.mjs';

const ms = (text) => parseFloat(String(text));
const pretty = (value) => `${value < 10 ? Math.round(value * 10) / 10 : Math.round(value)} ms`;

export default {
  id: 'follower-reads',
  title: 'Follower reads',
  premise: 'Read from the nearest copy instead of crossing an ocean.',

  async prepare(d) {
    const s = await clusterFacts(d);
    const scenario = await startScenario(d, 'follower-reads');
    const far = scenario.steps[0].label.replace(/^Normal read from /, '');     // the scenario picks the farthest region
    d.fact('far', far);
    d.fact('farPlace', s.regions.find((r) => r.name === far).label);
  },

  async run(d) {
    const f = d.facts;
    d.check((await d.state()).cluster.survival_goal === 'region', 'every table has copies in every region');
    await d.say('One cluster in {regions} {cloud} regions. Every table has copies in all of them.');
    await d.shot('scenario');
    await d.say('One copy, the leaseholder, answers normal reads. For this price table it is in {primaryPlace}.');

    const normal = (await d.step()).result;                          // Normal read from the far region
    const strong = ms(normal[`Read from ${f.far}`]);
    d.check(normal['Served by'] === `leaseholder in ${f.primary}`, 'the normal read was served by the leaseholder in the primary region');
    d.fact('strong', pretty(strong));
    await d.say('A user in {farPlace} reads a price. The query travels to {primaryPlace} and back: {strong}.');

    const follower = (await d.step()).result;                        // Follower read from the far region
    const local = ms(follower[`Follower read from ${f.far}`]);
    d.check(follower['Served by'] === `local replica in ${f.far}` && local < strong, 'the follower read was served locally and was faster');
    d.fact('follower', pretty(local));
    d.fact('faster', `${Math.round(strong / Math.max(local, 0.1))} times`);
    await d.say('Asked as a follower read, the copy in {farPlace} answers: {follower}.');
    await d.say('That is {faster} faster. The trade: the answer may be a few seconds old.');
    await d.say('One clause does it: AS OF SYSTEM TIME follower_read_timestamp().');

    await d.shot('wide');
    await d.say('For dashboards, catalogs and reports, a few seconds old is a fair trade for local speed.');
  },

  outro: (f) => [`Normal read from ${f.farPlace}: ${f.strong}`, `Follower read from ${f.farPlace}: ${f.follower}`, `${f.faster} faster, a few seconds stale`],
};
