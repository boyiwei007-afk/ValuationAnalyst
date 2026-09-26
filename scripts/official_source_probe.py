"""Fetch a discovered annual report through the same bounded research tools."""
import hashlib
import json
from datetime import date
from pathlib import Path

from valuationagent.application.research import ResearchService
from valuationagent.schemas.research import DocumentSummary, ResearchDraft
from valuationagent.search.providers import CninfoAnnouncementProvider
from valuationagent.storage.sqlite import SQLiteRunStore


def main():
    output = Path('var/delivery-source-20260925')
    store = SQLiteRunStore(output)
    service = ResearchService(store)
    session = service.create()
    session.draft = ResearchDraft(company='比亚迪', ticker='002594.SZ', valuation_date=date(2025, 6, 30))
    query = {'ticker': '002594.SZ', 'years': [2024], 'cutoff': '2025-06-30'}
    search = service._tool(session, 'search_sources', query,
        lambda: CninfoAnnouncementProvider().search_annual_reports('002594.SZ', [2024], cutoff=date(2025, 6, 30), company_name='比亚迪').model_dump(mode='json'))
    if search['status'] != 'completed':
        raise RuntimeError('Official search failed: ' + search['status'])
    hit = search['hits'][0]
    fid = 'web_' + hit['source_id']
    text = hit['title'] + '\n' + hit['snippet']
    session.documents.append(DocumentSummary(file_id=fid, name=hit['title'], role='evidence', block_count=1,
        sha256=hashlib.sha256(text.encode()).hexdigest(), size_bytes=len(text.encode())))
    store.save_research_blocks(session.session_id, fid, [{'block_id': fid + ':1', 'file_id': fid, 'text': text,
        'location': {'source_type': 'web_search', 'url': hit['url'], 'published_at': hit['published_at'],
                     'provider': search['provider'], 'search_query': query}}])
    fetched = service._tool(session, 'fetch_search_source', {'file_id': fid}, lambda: service._fetch_search_source(session, fid))
    store.save_research(session)
    blocks = store.research_blocks(session.session_id, fetched['file_id'])
    snippets = [b for b in blocks if '营业收入' in b['text'] and ('2024' in b['text'] or '归属于上市公司股东' in b['text'])][:3]
    result = {'search': search['status'], 'download': fetched, 'sha256': store.get_file(fetched['file_id'])['sha256'],
              'source_metadata_present': all(b['location'].get('published_at') == hit['published_at'] and b['location'].get('source_url') for b in blocks),
              'financial_blocks': snippets, 'session_id': session.session_id, 'scope': '定位并读取官方年报，不构成整家公司估值验收'}
    (output / 'source-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
