// Survive a zone outage: one availability zone loses power under load; its region keeps serving and no money is lost.
import { survive } from './_survive.mjs';

export default survive({
  id: 'survive-zone',
  title: 'Survive a zone outage',
  premise: 'An availability zone loses power while payments are flowing.',
  kind: 'zone',
});
