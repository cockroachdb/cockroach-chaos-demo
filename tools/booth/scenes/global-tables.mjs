// GLOBAL tables: read-mostly reference data that every region reads locally and consistently; writes cost more.
import { clusterFacts, startScenario } from './_common.mjs';

const ms = (text) => parseFloat(String(text));

export default {
  id: 'global-tables',
  title: 'Global tables',
  premise: 'Reference data that every region reads locally, never stale.',

  async prepare(d) {
    await clusterFacts(d);
    await startScenario(d, 'global-tables');
  },

  async run(d) {
    const f = d.facts;
    await d.say('Some data is read everywhere and rarely changes: exchange rates, product catalogs, settings.');
    await d.shot('scenario');
    await d.say('This exchange-rate table is a GLOBAL table, in a cluster across {regions} {cloud} regions.');

    const reads = Object.values((await d.step()).result).map(ms);    // Read from every region
    d.check(reads.length === f.regions, 'the rate was read in every region');
    d.fact('slowestRead', Math.ceil(Math.max(...reads)));
    await d.say('Reading a rate takes {slowestRead} ms or less in every region. No region has to ask another.');
    await d.say('And these are not stale copies: every region sees the latest committed rate.');

    const write = ms((await d.step()).result.Write);                 // Write once
    d.check(write > Math.max(...reads), 'the write cost more than any read');
    d.fact('write', `${Math.round(write)} ms`);
    await d.say('The cost moves to the write: updating one rate took {write}.');
    await d.say('The write waits, so that every region can keep answering locally.');

    await d.shot('wide');
    await d.say('For data that is read constantly and changed rarely, that is the right trade.');
  },

  outro: (f) => [`Reads in ${f.slowestRead} ms or less in all ${f.regions} regions`, 'Always the latest committed value', `One write: ${f.write}`],
};
