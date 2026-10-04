"""Plugin 2 - IFRS 17 / Regulatory Automation Engine.

Fewer than 10% of insurers have fully automated IFRS 17, and about half of those
still depend on significant manual intervention. No PAS vendor ships it natively,
because IFRS 17 is an accounting and actuarial problem rather than an
administration one - which is exactly why it belongs in a sidecar.

Components
----------
1. **CSM calculation engine** - GMM, VFA and PAA, with the roll-forward that
   actuaries are asked to sign off.
2. **Grouping (GIC) engine** - IFRS 17 paragraph 4 grouping, driven by a
   configurable rule engine rather than hard-coded logic.
3. **Cash-flow projection engine** - deterministic fulfilment cash flows plus a
   Monte Carlo risk adjustment with locked-in and updated discount-rate support.
4. **Disclosure generator** - IFRS 17 liability-for-remaining-coverage and
   liability-for-incurred-claims roll-forwards, plus a NAIC SSAP 102 mapping.
5. **PAS integration** - pulls policy, premium, claim and lapse data through
   plugin 1's atomic operations and writes reserves back.

Every number this plugin produces carries its inputs, its assumption version and
its intermediate steps. An actuarial result that cannot be reconstructed is not
usable in a filing.
"""

from __future__ import annotations

from pas_plugins.plugin2_ifrs17.settings import Plugin2Settings

__all__ = ["Plugin2Settings"]
