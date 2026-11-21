// REGIONAL BY ROW: one table, each customer's row stored in their home region; local reads and writes are fast.
import { clusterFacts, startScenario } from './_common.mjs';

/** { local: slowest cell on the diagonal, far: slowest cell off it, nearest: fastest cell off it } in ms. */
function spread(matrix) {
  const local = []; const far = [];
  for (const [gateway, row] of Object.entries(matrix)) for (const [home, ms] of Object.entries(row)) (gateway === home ? local : far).push(ms);
  return { local: Math.max(...local), far: Math.max(...far), nearest: Math.min(...far) };
}

export default {
  id: 'regional-by-row',
  title: 'Regional by row',
  premise: "One table. Each customer's row lives in their own region.",

  async prepare(d) {
    await clusterFacts(d);
    await startScenario(d, 'regional-by-row');
  },

  async run(d) {
    await d.say('One cluster in {regions} {cloud} regions: {places}.');
    await d.shot('scenario');
    await d.say("A customers table is REGIONAL BY ROW: each customer's row is stored in their home region.");

    const reads = spread((await d.step()).result.matrix);            // Read from every region
    d.check(reads.local < reads.nearest, 'reading a row at home is faster than reading it from any other region');
    d.fact('localRead', Math.ceil(reads.local));
    d.fact('farRead', Math.round(reads.far));
    await d.say('Down the side: where the query runs. Across the top: where the row lives.');
    await d.say('The diagonal is a customer reading their own row at home: {localRead} ms or less.');
    await d.say('Off the diagonal the row lives in another region: up to {farRead} ms, the round trip.');

    const writes = spread((await d.step()).result.matrix);           // Write from every region
    d.check(writes.local < writes.nearest, 'writing a row at home is faster than writing it from any other region');
    d.fact('localWrite', Math.ceil(writes.local));
    d.fact('farWrite', Math.round(writes.far));
    await d.say('Writes follow the same rule: {localWrite} ms or less at home, up to {farWrite} ms from far away.');
    // A write at home is slower than a read at home when the database must survive a region: say why.
    if ((await d.state()).cluster.survival_goal === 'region' && writes.local > 4 * reads.local) {
      await d.say('Why not {localRead} ms? This database survives a region outage: a second region confirms every write.');
    }

    await d.shot('wide');
    await d.say('Same table, same SQL. Every customer reads their own data at local speed.');
  },

  outro: (f) => [`One table across ${f.regions} regions`, `At home: reads in ${f.localRead} ms or less, writes in ${f.localWrite} ms or less`,
    'Each row is stored where its customer is'],
};
