Adding an internal app to the foundation

What a new app may change in foundation/

- Allowed without justification: adding the app's permission entries to 
- foundation/policy.yaml, and adding its nav link to foundation/ui/base.html.
- Anything else in foundation/ (shared code, partials, helpers) may be changed only if 
- the task requires it, and the PR description must explain what changed and why. 
- Prefer adding to the app over changing shared code.

Steps

1. Create apps/<name>/ with models.py, routes.py, templates/, rules.py.
2. Models. SQLAlchemy models in models.py. Add an Alembic migration. 
3. Every status column must have its allowed values listed in rules.py.
4. Permissions. Add the app's actions to foundation/policy.yaml as 
5. <name>.<verb>: [roles]. Every route must use Depends(require("<name>.<verb>")). 
6. The route-coverage test fails the build if any non-public route lacks this.
7. State changes. Use foundation.workflow.transition(...) for every status change. 
8. Never write obj.status = ... directly. Approval steps must pass 
9. forbid_actor=<requester_id> so a user cannot approve their own request.
10. Audit. Every write calls foundation.audit.record(session, ...) in the same 
11. session before commit. Do not commit inside record.
12. Concurrency. Any check-then-write against shared state (balances, limits, counts) 
13. must lock the parent row (SELECT ... FOR UPDATE) or use a conditional update, 
14. and must have a concurrent test.
15. External systems. Never call a vendor SDK from a route. Define or reuse a 
16. protocol in foundation/connectors/ and inject it. Never call an external system 
17. inside a DB transaction.
18. UI. Templates extend foundation/ui/base.html and use the shared table, 
19. filter and summary-card partials. Show only actions the current user is 
20. permitted to take (can(user, action) helper), but never rely on this for security.
21. Register the router in main.py and add the nav link in base.html.
22. Seed data in apps/<name>/seed.py, called from the global seed command.
23. Tests in tests/<name>/: every rule in rules.py, every invalid transition, 
24. the self-approval case if the app has approvals, and a concurrent test for any 
25. concurrency rule described above. The policy matrix and route-coverage tests pick up new routes automatically.
26. PR. One PR per session. Description includes: the prompt you were given, 
27. what you built, test results, any change to foundation/ beyond step "Allowed", 
28. and anything you were unsure about.

Definition of done: docker compose up works, pytest passes locally and in CI, 
and the new app appears in the nav with seeded data.
