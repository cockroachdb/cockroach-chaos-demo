// Data domiciling: super regions keep every copy of a customer's data in their home region. A lost node there is
// survived; a lost region pauses only its own customers.
import { clusterFacts, list, seconds, startScenario } from './_common.mjs';

/** "3 in eu-west-1, 2 outside" -> { home: 3, outside: 2 } ("none outside" -> 0). */
const copies = (text) => { const m = /^(\d+) in \S+, (\w+) outside$/.exec(text); return m ? { home: Number(m[1]), outside: m[2] === 'none' ? 0 : Number(m[2]) } : null; };

export default {
  id: 'data-domicile',
  title: 'Data domiciling',
  premise: "Keep every copy of a customer's data in their own region.",

  async prepare(d) {
    const s = await clusterFacts(d);
    const scenario = await startScenario(d, 'data-domicile');
    const abroad = scenario.steps[2].label.replace(/^Lose a node in /, '');     // the scenario picks the region and the node
    d.fact('abroad', abroad);
    d.fact('abroadPlace', s.regions.find((r) => r.name === abroad).label);
    d.fact('victim', scenario.steps[3].label.replace(/^Bring /, '').replace(/ back$/, ''));
    d.fact('elsewhere', list(s.regions.filter((r) => r.name !== abroad).map((r) => r.label)));
  },

  async run(d) {
    const f = d.facts;
    await d.say('One CockroachDB cluster across {regions} {cloud} regions: {places}.');
    await d.say('Some data must stay where its owner lives. Privacy rules often say so.');

    const create = await d.next();                                   // Create customers
    await d.say('One customers table, with customers in every region.', { wait: false });
    const before = (await create.done({ timeout: 900 })).result;
    const who = Object.keys(before).find((k) => String(before[k]).includes(` in ${f.abroad},`));   // "Aoife (Dublin)"
    d.check(Boolean(who) && copies(before[who]) && copies(before[who]).outside > 0, 'by default a row also has copies outside its home region');
    d.fact('name', who.split(' (')[0]);
    d.fact('city', who.split(' (')[1].replace(')', ''));
    d.fact('defaultHome', copies(before[who]).home);
    d.fact('defaultOutside', copies(before[who]).outside);
    await d.shot('scenario');
    await d.say('{name} is a customer in {city}. Data about {name} belongs in {abroadPlace}.');
    await d.say('By default the row has {defaultHome} copies in {abroadPlace}, and {defaultOutside} more in other regions for fast reads.');
    await d.say('Fast everywhere. But the data has left {abroadPlace}.');

    const restrict = await d.next();                                 // Keep data at home
    await d.say('One statement per region makes each a super region: data stored there never leaves it.', { wait: false });
    const after = (await restrict.done({ timeout: 900 })).result;
    d.check(after['Copies outside their home region'] === '0' && copies(after[who]).outside === 0, 'after the super regions no copy is outside its home region');
    d.fact('homeNow', copies(after[who]).home);
    await d.say("Now {name}'s row has {homeNow} copies in {abroadPlace} and none anywhere else.");
    await d.say("The same holds for every customer: no copy of anyone's data is outside their home region.");

    await d.shot('wide');
    await d.say('Does staying home make the data fragile? Take a node away in {abroadPlace}.');
    const node = await d.next();                                     // Lose a node in the region
    await d.shot(`region:${f.abroad}`);
    const nodeDown = (await node.done({ lapse: false })).result;
    d.check(Object.values(nodeDown)[0] === 'committed', "the customer's row was still written with a node down");
    d.check(Object.values(nodeDown)[1] === '0', 'no copy was placed outside the region to replace the lost one');
    await d.say("One of three nodes in {abroadPlace} is down. {name}'s row can still be written: two copies remain.");
    await d.say('And no copy was sent abroad to replace the third. The rule holds under failure.');

    await d.shot('wide');
    const nodeBack = await d.next();                                 // Bring the node back
    await d.say('The node returns, and the third copy is home again.', { wait: false });
    await nodeBack.done({ lapse: false });
    await d.until((s) => s.cluster.status === 'healthy' && s.cluster.live_nodes === s.cluster.total_nodes, { timeout: 240, lapse: true, what: 'the node to come back' });

    await d.say('Now the hard case: all of {abroadPlace} goes dark.');
    const region = await d.next();                                   // Lose the whole region
    await d.shot(`region:${f.abroad}`);
    const regionDown = (await region.done({ timeout: 300 })).result;
    const theirs = Object.entries(regionDown).find(([k]) => k.endsWith(`(${f.abroad})`));
    const rest = Object.entries(regionDown).filter(([k]) => k.endsWith(')') && !k.endsWith(`(${f.abroad})`));
    d.check(theirs && String(theirs[1]).startsWith('waits for'), "the lost region's customer waits for it");
    d.check(rest.length > 0 && rest.every(([, v]) => v === 'committed'), 'customers in the other regions were still written');
    const payments = /^recovered in (\d+ s), (\$\d+) lost$/.exec(regionDown.Payments || '');
    await d.shot('scenario');
    await d.say('Customers in {elsewhere} carry on: their writes commit.');
    await d.say("{name}'s row is unavailable until {abroadPlace} returns. No copy exists anywhere else, by design.");
    if (payments && payments[2] === '$0') d.fact('recovery', seconds(payments[1]));   // kept as a fact; the story stays on the customers

    await d.shot('wide');
    const back = await d.next();                                     // Bring the region back
    await d.say('{abroadPlace} returns.', { wait: false });
    const returned = (await back.done({ timeout: 600 })).result;
    d.check(Object.values(returned)[0] === 'committed again', "the customer's row can be written again");
    await d.until((s) => s.cluster.status === 'healthy' && s.cluster.live_nodes === s.cluster.total_nodes, { timeout: 240, lapse: true, what: 'the region to come back' });
    await d.say("{name}'s row can be written again. It never left {abroadPlace}.");
    await d.say('The trade: data kept at home survives a lost node or data center, not the loss of its whole region.');
  },

  outro: (f) => [`Every copy of ${f.name}'s data stayed in ${f.abroadPlace}`, 'A node lost there: still writable, nothing sent abroad', `${f.abroadPlace} lost: only its own customers waited`],
};
