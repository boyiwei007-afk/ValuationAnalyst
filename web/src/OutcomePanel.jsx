import Icon from './Icons'

const stages = [['范围', 'Scope'], ['找数', 'Sources'], ['建模', 'Model'], ['报告', 'Report']]

export default function OutcomePanel({ session, report, busy, exporting, t, onDownload, onStart, onConnect, connected }) {
  const ready = Boolean(session?.question?.valuation_review)
  const complete = report?.numeric_result_available
  const explained = report && !complete && !busy
  const step = complete ? 3 : ready ? 2 : session?.documents?.length ? 2 : session?.draft?.company || session?.draft?.ticker ? 1 : 0
  const title = busy ? t('正在推进估值', 'Valuation in progress') : complete ? t('估值结果已生成', 'Valuation complete') : ready ? t('确认方案，即可计算', 'Review the plan to calculate') : explained ? t('结果说明已生成', 'Outcome report available') : t('你的下一份估值报告', 'Your next valuation report')
  return <div className="outcome-panel">
    <div className="outcome-kicker"><span><Icon name="file" size={16}/>VALUATION BRIEF</span><span className={`outcome-badge ${complete ? 'complete' : ''}`}>{busy ? t('执行中', 'In progress') : complete ? t('已计算', 'Calculated') : explained ? t('已出说明', 'Explained') : t('待完成', 'Pending')}</span></div>
    <h3>{title}</h3>
    <p className="outcome-description">{busy ? t('正在查找并核验必要数据。遇到关键缺口，也会交付带依据的说明报告。', 'Gathering and checking required inputs. An evidence-based report is available even if data is missing.') : report?.conclusion || t('输入公司名称即可开始，无需先准备文件。系统将自动检索、核对数据，并生成可下载的结果文档。', 'Start with a company name. No files required. The Agent retrieves and checks data, then delivers a downloadable outcome.')}</p>
    <ol className="outcome-stages" aria-label={t('估值步骤', 'Valuation stages')}>{stages.map(([zh, en], i) => <li key={en} className={i < step || complete ? 'done' : i === step ? 'current' : ''} aria-current={i === step ? 'step' : undefined}><span>{i < step || complete ? <Icon name="check" size={12}/> : i + 1}</span>{t(zh, en)}</li>)}</ol>
    {report && <><div className="outcome-facts"><span><b>{report.counts?.verified || 0}</b>{t('已确认字段', 'confirmed inputs')}</span><span><b>{report.counts?.sources || 0}</b>{t('来源线索与原文', 'leads & sources')}</span><span><b>{report.counts?.searches || 0}</b>{t('检索记录', 'search attempts')}</span></div>
      {!complete && <div className="outcome-limitation"><Icon name="shield" size={16}/><span>{t('说明报告不等于数值估值。缺失值不会按零计算，未核验信息不会作为财务事实。', 'An explanatory report is not a price estimate. Missing values are not treated as zero; unverified information is excluded.')}</span></div>}
      <div className="outcome-downloads"><button disabled={Boolean(exporting)} onClick={() => onDownload('pdf')} className="button primary"><Icon name="download" size={16}/>{exporting === 'pdf' ? t('正在生成…', 'Generating…') : t('下载 PDF 报告', 'Download PDF')}</button><button disabled={Boolean(exporting)} onClick={() => onDownload('html')} className="button secondary">HTML</button><button disabled={Boolean(exporting)} onClick={() => onDownload('json')} className="button secondary">JSON</button></div>
      <p className="outcome-version">v{report.source_revision} · {t('已保存，可随时下载', 'Saved and downloadable')}{session && session.revision > report.source_revision && t(' · 当前有新进度，下载时更新', ' · New progress will be included on export')}</p>
    </>}
    {!report && <div className="outcome-promises"><p><Icon name="globe" size={17}/>{t('优先官方披露，保留来源', 'Official disclosures with source records')}</p><p><Icon name="chart" size={17}/>{t('适用方法、区间与敏感性', 'Suitable methods, ranges and sensitivity')}</p><p><Icon name="file" size={17}/>{t('数据不足也交付说明文档', 'An explanatory report when data is limited')}</p></div>}
    {!busy && !connected && <button className="button secondary outcome-connect" onClick={onConnect}><Icon name="link" size={16}/>{t('连接模型，启用自动研究', 'Connect a model to automate research')}</button>}
    {session && !busy && !session.question && !complete && <button className="research-text-button" onClick={onStart}>{t('继续补齐数据并估值', 'Continue research and valuation')}<Icon name="arrow" size={14}/></button>}
    {ready && <p className="outcome-next">{t('在对话中确认整套方案后，计算会自动继续。', 'Confirm the combined plan in the conversation to continue automatically.')}</p>}
  </div>
}
