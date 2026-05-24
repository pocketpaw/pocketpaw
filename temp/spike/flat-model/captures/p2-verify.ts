// p2-verify.ts — apply P2b's flat patch to the broken pocket and verify the result
// renders correctly. Sanity check for the Phase 2 finding.

import { flatten, merge, unflatten, type UISpec } from '../flatten.ts';
import brokenPocket from './broken-edit-pocket.json' assert { type: 'json' };

// Strip _capture_context (test-only metadata) before treating as spec.
const { _capture_context, _pocket_id, _pocket_name, ...spec } = brokenPocket as unknown as Record<string, unknown> & UISpec;

// Step 1: flatten the broken spec into FlatSpec form.
const flat = flatten(spec as UISpec);

// Step 2: apply P2b's flat patch (verbatim from /tmp/p2b-flat.json).
const p2bPatch = {
  components: {
    n_no719i8s: {
      type: 'kanban',
      id: 'n_no719i8s',
      bind: 'cards',
      props: {
        columnKey: 'status',
        columns: [
          { id: 'lead',      title: 'Lead' },
          { id: 'qualified', title: 'Qualified' },
          { id: 'proposal',  title: 'Proposal' },
          { id: 'won',       title: 'Won' },
        ],
      },
    },
  },
};

const patched = merge(flat, p2bPatch as any);
const back = unflatten(patched);

// Walk to find the kanban widget — should have the new columns.
function findById(node: any, id: string): any {
  if (!node) return null;
  if (node.id === id) return node;
  for (const c of node.children ?? []) {
    const hit = findById(c, id);
    if (hit) return hit;
  }
  return null;
}

const kanban = findById(back.ui, 'n_no719i8s');
const cardsState = (back as any).state?.cards;

console.log('=== P2b patch verification ===');
console.log('kanban widget found:', !!kanban);
console.log('kanban.bind preserved:', kanban?.bind === 'cards' ? '✓' : `✗ got ${kanban?.bind}`);
console.log('kanban.props.columnKey preserved:', kanban?.props?.columnKey === 'status' ? '✓' : `✗ got ${kanban?.props?.columnKey}`);
console.log('kanban.props.columns updated:', JSON.stringify(kanban?.props?.columns?.map((c: any) => c.id)));
console.log('cards in state count:', cardsState?.length);
console.log('cards reachable to lanes:');
const colIds = new Set(kanban?.props?.columns?.map((c: any) => c.id));
const orphanedCards = (cardsState ?? []).filter((card: any) => !colIds.has(card.status));
console.log('  orphaned (status not in any column):', orphanedCards.length);
console.log('  routed correctly:', cardsState.length - orphanedCards.length, '/', cardsState.length);

if (orphanedCards.length === 0 && kanban?.bind === 'cards') {
  console.log('\nVERDICT: P2b patch fixes the broken pocket. All 8 cards route to lanes. bind preserved.');
} else {
  console.log('\nVERDICT: P2b patch did NOT fully fix the pocket. Inspect above.');
}
