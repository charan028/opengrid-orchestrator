"""ogsim.utility_aen -- simulated utility EMS for the D-29 tolling contract: Austin Energy (default) calls
discharge on its toll obligation at its evening peak on hot days, through a pluggable `Channel`
(`ogsim.utility_aen.channels`). Shares no code with `opengrid`; it meets the orchestrator only at the
wire (the utility customer API, or the grid link)."""
