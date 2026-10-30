# layer-keys lead record

Issue: #1036 · Packet: docs/blueprints/halo/features/layer-keys.md
Run: act-1a13eb691a3-wg4n · Session: e0e565d9-6109-5c52-89fd-c254398040f9 · Started: 2026-10-15T08:39Z
Stage: 5 finish (blocked: dev red on #1185, not ours)
Base: a476a74
Next: when #1185 merges, run round 2 (same lenses + audit) on the first dev run past swift test, then post Belt-Contact-Sheet on #1036.

## Tasks
| id | wave | behavior | files | test | PR | state |
|---|---|---|---|---|---|---|
| LK-1 | 1 | HaloKeyRules sampling, matrices, follows; LiveLayerPlacing real; FakeLayerPlacing on the rules | Live/HaloKeyRules, Live/LiveLayerPlacing, Fakes/FakeLayerPlacing, LayerPlacingContractTests | LayerPlacingContractTests [fake, live], HF-3, HF-5 | #1054 | merged |
| LK-2a | 2 | read half: keyState, rows; LivePropertyKeying rows real | Live/HaloKeyRules, Live/LivePropertyKeying, Fakes/FakePropertyKeying, PropertyKeyingContractTests | PropertyKeyingContractTests (rows) | #1064 | merged |
| LK-2b | 3 | write half: setValue, toggleAnimation, toggleKey | same as LK-2a | PropertyKeyingContractTests (writes) | #1077 | merged |
| LK-3 | 4 | setParent in the rules, cycle refused, child kept on screen | same as LK-2a | PropertyKeyingContractTests on parented | #1088 | merged |
| LK-4 | 4 | LiveViewerDragging hit and commitMove; fake wraps it | Live/LiveViewerDragging, Fakes/FakeViewerDragging, ViewerDraggingContractTests | ViewerDraggingContractTests | #1086 | merged |
| LK-5 | 4 | propertyRows, setProperty to/text, endPropertyScrub, shown helpers | HaloKeying.swift, HaloKeyingTests | HaloKeyingTests | #1091 | merged |
| LK-6 | 5 | toggles, setParent, choices; keys availability | HaloKeying.swift, Availability/HaloAvailability+Keys, HaloKeyToggleTests, HaloKeysAvailabilityTests | both suites | #1109 | merged |
| LK-7 | 5 | compositionPoint and viewer drag begin/move/end/cancel | HaloViewerDrag.swift, HaloKeyByDragTests | HaloKeyByDragTests | #1105 | merged |
| LK-8 | 6 | budgets HB-1, HB-3, HB-4 | HaloKeysBudgetTests | HaloKeysBudgetTests | #1118 | merged |
| LK-9 | 6 | keys routes; TRANSFORM rows, key buttons, refusal caption | HaloApp/HaloWindow+Keys, HaloApp/HaloLayerInspector, HaloInspectorSnapshots | halo-inspector(-refusal) snapshots | #1117 | merged |
| LK-11 | 6 | HaloViewerGestures drag and Escape | HaloApp/HaloViewerGestures, HaloKeyByDragSnapshots | halo-key-by-drag snapshots | #1115 | merged |
| LK-10 | 7 | LAYER group Parent and Blend; narrow snapshot | HaloApp/HaloLayerInspector, HaloInspectorSnapshots | halo-inspector-layer, -narrow snapshots | #1119 | merged |
| LK-12 | 7 | H-J5 drag journey | UITests/HaloJourneyTests+Drag | H-J5 | #1122 | merged |
| LK-13 | 7 | H-J6 inspector journey | UITests/HaloJourneyTests+Inspector | H-J6 | #1126 | merged |
| LK-14 | 8 | H-J23 parent journey, feature-done | UITests/HaloJourneyTests+Parent | H-J23 | #1129 | merged |

Waves as in the packet. Independent per wave: W1 LK-1; W2 LK-2a; W3 LK-2b; W4 LK-3, LK-4, LK-5; W5 LK-6, LK-7; W6 LK-8, LK-9, LK-11; W7 LK-10, LK-12, LK-13; W8 LK-14.

## Rounds
| round | CI run | CI | audit vs baseline | lenses | confirmed | fixed by | clean |
|---|---|---|---|---|---|---|---|
| 1 | 38734211999 (35a875b) | red: swift test fails only HaloViewerFidelityTests.keyedFrame6 (keyed frame 6, 40000 px differ; HF-1 from #1125); journeys did not run. All our snapshots rendered. (Earlier 38732179619: playback test compile, fixed by #1136) | – (no ui-screens) | clarity, journey, design, layout, controls | 3 major (one finding: narrow TRANSFORM labels clip), 6 minor | #1153 (TRANSFORM stacks key buttons under bars via ViewThatFits at narrow widths; layer snapshot at frame 0); SuiteUI slider ticks/marker → #1146 | no |
