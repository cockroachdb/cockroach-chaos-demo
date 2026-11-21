// Split-brain test: a region is cut off from the others; the majority keeps committing, the isolated side refuses,
// and after healing both sides agree.
import { clusterFacts, list, startScenario } from './_common.mjs';

export default {
  id: 'split-brain',
  title: 'Split-brain test',
  premise: 'Cut a region off from the others. Can the two sides disagree?',

  async prepare(d) {
    const s = await clusterFacts(d);
    const scenario = await startScenario(d, 'split-brain');
    const far = scenario.steps[0].label.replace(/^Isolate /, '');     // the scenario picks the region to cut off
    d.fact('far', far);
    d.fact('farPlace', s.regions.find((r) => r.name === far).label);
    d.fact('majority', list(s.regions.filter((r) => r.name !== far).map((r) => r.label)));
  },

  async run(d) {
    const f = d.facts;
    await d.say('One cluster in {regions} {cloud} regions. Networks fail: what if one region is cut off?');
    await d.say('The danger is split brain: both sides keep accepting writes, and the data forks.');

    const cut = await d.next();                                      // Isolate the far region
    await cut.done({ lapse: false });
    await d.shot(`region:${f.far}`);
    await d.until((s) => s.nodes.filter((n) => n.region === f.far).every((n) => n.isolated), { timeout: 60, what: 'the region to be isolated' });
    await d.say('{farPlace} can no longer reach the other regions. Its own clients can still reach it.');

    await d.shot('scenario');
    await d.say('Now send a write to each side.');
    const writes = (await d.step()).result;                          // Write on both sides
    d.check(writes[`Write via ${f.primary}`] === 'committed', 'the majority side committed its write');
    d.check(String(writes[`Write via ${f.far}`]).startsWith('refused'), 'the isolated side refused its write');
    await d.say('The side with a majority, {majority}, commits.');
    await d.say('{farPlace} refuses. It cannot reach a majority of the copies, so it will not guess.');

    await d.say('Now the network heals. Compare what each side sees.');
    const healed = (await d.step()).result;                          // Heal and compare
    d.check(String(healed.Diverged).startsWith('no'), 'the two sides did not diverge');
    d.fact('rows', healed[`Rows seen from ${f.primary}`]);
    d.check(healed[`Rows seen from ${f.far}`] === f.rows, 'both sides see the same rows');
    await d.say(f.rows === 1 ? 'Both sides see the same single row: the one the majority committed.' : 'Both sides see the same {rows} rows. Nothing diverged.');

    await d.shot('wide');
    await d.until((s) => s.cluster.status === 'healthy' && s.nodes.every((n) => !n.isolated), { timeout: 120, what: 'the cluster to be whole' });
    await d.say('A network split can cost the cut-off side its writes. It never creates two versions of the truth.');
  },

  outro: () => ['A region was cut off from the cluster', 'The majority kept committing; the cut-off region refused', 'After healing, both sides agree'],
};
