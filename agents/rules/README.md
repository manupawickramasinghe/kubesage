# Custom rule sets

Every `.txt` or `.md` file in this folder (except this README and files starting with `_` or `.`) is read on
every agent-team run and given, verbatim, to the **Thinker** and the **Executor**. Write in plain English:
what's special about a workload, what must never change, how scheduling works here. The agents treat these
rules as overriding their defaults, and must say in the PR when a rule changed what they recommend.

Files are read in name order. Edit them on the server; the web UI only shows them.
