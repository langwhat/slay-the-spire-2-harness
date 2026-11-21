# Shared execution runs whole decision units

Users can pause the run at any time, and the pause has to land in a clean place: after a decision and its action batch have fully played out, including settlement and feedback, never halfway through. Automatic progression takes one action per unit.

So `Execution.run_unit(source, task)` runs the whole unit itself. It takes any decision source (a model agent, fixed code, or automatic progression), asks it for a decision, submits and settles the result, and returns the feedback to the same source. Orchestration only picks the source and the task. Run decides internally, from the admitted observation, whether automatic progression or its strategy acts.

This keeps pause handling and submission order the same for every kind of decision-maker, without tying execution to a model. Agents keep their own model and tool-loop state; proposals are plain data. "Unit" here means a pause boundary. It does not mean rollback or a guarantee that every proposed action completes.
