# DECISIONS.md

1. **Indexes.** On `employees`: `emp_code` (unique, blocks duplicates), `department + emp_code` (department filter, trend) and `joined_on` (summary headcount). On `attendance_logs`: `emp_code + date` (unique; punch-in, monthly summary, lookups), `date + emp_code` (attendance list, leaderboard) and `emp_code + punch_in` (punch-out finds the last punch-in). I skipped an index on `status`: only five values, so it barely narrows anything.

2. **Punch-in race.** Both requests work out the same date. I don't check first; both insert, and the unique `emp_code + date` index lets only one succeed. The other gets a duplicate key error, which I return as 409.

3. **Ties.** I use `$rank`, so tied people get the same rank and the next rank is skipped (1, 1, 1, 4). I keep everyone with rank up to `limit`, so all tied people are included even if that is more than `limit` rows.

4. **Headcount.** The pipeline starts from `employees`, not logs, and then joins each person's logs. A person with no logs just has an empty list but is still counted.

5. **100x data.** I would keep a small daily total per department, updated on each punch, so reports do not read every log.