1) cp config.sh.example config.sh
2) vim config.sh
3) virtualenv .venv
4) source .venv/bin/activate
5) source config.sh
6) PYTHONPATH=. python lib/jira_wrapper.py
7) PYTHONPATH=. python lib/backport_analyzer.py
