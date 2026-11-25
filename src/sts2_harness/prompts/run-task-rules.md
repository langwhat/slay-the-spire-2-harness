Several tools may be called in one response; they run in order and their results return together. At most one of them may be a game action (run_act or run_next), and it comes last. Submit no game action during a handoff, during battle, or while control is not ready. The current task and state decide what to do, not an earlier plan.

If a run_plan result reports that the interface was not confirmed restored, submit no game action and no run_plan until the program confirms it; reading remains allowed.

An automatic-progress report describes operations the program already performed. Do not repeat them; continue from the current state.
