# Human-reviewed tests

Static checks of deliberate design decisions, such as which fields name an
anonymous tunnel.

They mark decisions a maintainer must see, not code to design around. Agents
may change these tests, and the code they check, when a design calls for it;
never avoid or bend a design to keep them unchanged. But every change must be
highlighted to the maintainer: in the review or hand-over message, under its own
heading, saying which test or checked code changed, what it decided before and
after, and why. Never change them silently, and never weaken one just to make a
change pass.
