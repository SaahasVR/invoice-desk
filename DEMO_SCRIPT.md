# 5-minute demo: talking points

Say these in your own words. The video must be your voice, and it lands better unscripted. Rehearse the click order, not the sentences.

**Before recording:** open the live link, click **Reset my data**, use a clean browser window and close other tabs. Run one invoice off-camera to wake a sleeping free-tier server, then reset again. Your sandbox is private to your browser, so nothing a reviewer does can change what you see.

## 0:00 - 0:30 The problem and the idea
- "Invoices arrive as messy PDFs. AP matches each to a PO, checks the numbers and decides. Tired people make expensive mistakes."
- "I built a process that takes an invoice and returns a decision *with the reasoning visible*. Four outcomes: approve, review, hold, reject."
- Point at the three columns: inbox, audit trail, decision.

## 0:30 - 1:20 Happy path: `Clean invoice, exact PO match`
- Click it. Narrate each stage as it ticks green.
- "It doesn't just say yes. It tells me *why*: vendor approved, PO open, amount fits the remaining balance, no duplicate."
- Click **Original invoice** to show the PDF it read.

## 1:20 - 2:10 Edge case 1: duplicate with an altered number
- Run `Duplicate with altered invoice number`.
- "Same vendor, same amount, same date as an invoice already paid, but the number is `INV-2041A` instead of `INV-2041`. An exact-match check would miss it. Two independent signals fired: the near-identical number, and the PO is already fully billed."

## 2:10 - 3:00 Edge case 2: split PO
- Run `Split PO: second milestone invoice`: approved, balance tracked.
- Run `Split PO: invoice would overrun the PO`: needs review.
- "Vendors bill one PO in pieces. It tracks what's already been billed, not just the PO total."

## 3:00 - 3:50 Edge case 3 and the human in the loop
- Run `Over PO beyond tolerance` (or the credit note: "a credit note must never be paid").
- Type a note and click **Approve for payment**. "A person makes the call, it's logged with a note, and the ledger updates."
- Optional: re-run the same invoice to show it's now held as a duplicate of the approved one.

## 3:50 - 4:30 A bad scan
- Run `Poor phone photo of an invoice`: "OCR confidence is under half, so it doesn't guess. It routes to a person and says why." Contrast with the clean scan, which reconciles and passes.

## 4:30 - 5:00 Dashboard, rules, close
- **Dashboard:** runs, who resolved what, why invoices get flagged, the time-saved estimate ("8 minutes is my assumption").
- **Rules:** "Every rule in plain English with the live thresholds. An AP team tunes these, not an engineer."
- One limitation, honestly: "Vendor identity is name-based here; a real deployment would verify bank and tax ID, and tune extraction on the client's real invoices."

## If asked in the interview
- **Why rules plus an LLM?** Predictable and auditable on common layouts. The LLM fills gaps and, as a cross-check, flags any value where it disagrees with the rules. A disagreement goes to a person.
- **Why route errors to review?** An invoice must never be silently dropped or auto-paid because the process broke.
- **What would you change for a real client?** Thresholds with their AP team, vendor-master and ERP integration, then measure straight-through rate on their real invoices.
- **Why did you isolate each visitor?** Shared state made one reviewer's approved invoice turn another reviewer's into a "duplicate".
- **What does the Evaluation tab prove?** Only that the rules still produce the expected decisions on the cases they were built around. It is a regression check, not an accuracy score.
- **Do not claim** the LLM path was tested with a real model. The cross-check and gap-fill logic was tested against a simulated one.
