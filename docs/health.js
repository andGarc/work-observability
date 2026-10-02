// Pure workflow assessment shared by the dashboard and its tests.
function assessHealth(data, latest, schedule, now) {
  if (!latest) return {job: 'UNKNOWN', reason: 'No workflow runs returned.'};
  const due = new Date(now);
  due.setUTCHours(12, 17, 0, 0);
  due.setUTCDate(due.getUTCDate() - 1);
  const missed = now >= due.getTime() + 86400000 && (!schedule || new Date(schedule.created_at) < due);
  let job = 'PASS', reason = 'Latest workflow run completed and published matching JSON.';
  if (latest.status !== 'completed') { job = 'UNKNOWN'; reason = 'Latest workflow run is still in progress.'; }
  else if (latest.conclusion !== 'success') { job = 'FAIL'; reason = `Latest workflow run concluded ${latest.conclusion}.`; }
  else if (!data) { job = 'UNKNOWN'; reason = 'Published results.json could not be loaded.'; }
  else if (String(data.run_id) !== String(latest.id)) { job = 'FAIL'; reason = 'Latest successful run has no matching published JSON.'; }
  if (missed && job !== 'FAIL') { job = 'UNKNOWN'; reason = 'Scheduled run was missed and remains unresolved after the next daily run time.'; }
  return {job, reason};
}
function overallStatus(job, quality) {
  if (job === 'FAIL' || quality === 'FAIL') return 'FAIL';
  if (job === 'UNKNOWN' || quality === 'UNKNOWN') return 'UNKNOWN';
  if (job === 'WARN' || quality === 'WARN') return 'WARN';
  return 'PASS';
}
if (typeof module !== 'undefined') module.exports = {assessHealth, overallStatus};
