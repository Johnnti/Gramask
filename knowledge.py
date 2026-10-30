"""Small SQLite search index. Retrieval proposes evidence; Gemma selects it.

No external vector service or embedding model is required. This module accepts
connections/settings explicitly so tests and isolated demos cannot share state.
"""
import json
import re
import unicodedata


def question_identity(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split()).rstrip(' ?.!')


def init_index(connection, static_facts):
    connection.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_search USING
        fts5(doc_id UNINDEXED, content, tokenize='porter unicode61')""")
    # Seed edits and existing databases are reflected on every startup.
    connection.execute('DELETE FROM knowledge_search')
    connection.executemany('INSERT INTO knowledge_search(doc_id,content) VALUES (?,?)', [
        ('static:' + fact['id'], ' '.join([fact['title'], fact['value'], *fact.get('keywords', [])]))
        for fact in static_facts])
    connection.execute("""INSERT INTO knowledge_search(doc_id,content)
        SELECT 'live:' || id, question || ' ' || value FROM facts""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS knowledge_insert AFTER INSERT ON facts BEGIN
        INSERT INTO knowledge_search(doc_id,content) VALUES ('live:' || new.id, new.question || ' ' || new.value);
    END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS knowledge_delete AFTER DELETE ON facts BEGIN
        DELETE FROM knowledge_search WHERE doc_id='live:' || old.id;
    END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS knowledge_update AFTER UPDATE OF question,value ON facts BEGIN
        DELETE FROM knowledge_search WHERE doc_id='live:' || old.id;
        INSERT INTO knowledge_search(doc_id,content) VALUES ('live:' || new.id, new.question || ' ' || new.value);
    END""")


def fresh(fact, now, settings):
    return fact['created_at'] <= now < min(fact['valid_until'], fact['created_at'] + settings.live_ttl_seconds)


def usable_fact(fact, settings, is_usable):
    if not is_usable(fact['value']) or fact['confidence'] < settings.agreement:
        return False
    if fact.get('report_summary'):
        summary = json.loads(fact['report_summary'])
        answers = summary['answers']
        top = max((answer['count'] for answer in answers), default=0)
        leaders = [answer for answer in answers if answer['count'] == top]
        return top > 0 and len(leaders) == 1 and is_usable(leaders[0]['value'])
    return True


def retrieve(connection, text, owner, now, settings, static_facts, is_usable):
    # Quote tokens rather than interpreting the user's text as FTS query syntax.
    stop = {'a', 'an', 'the', 'is', 'are', 'was', 'were', 'do', 'does', 'did', 'of',
            'in', 'on', 'at', 'to', 'for', 'and', 'or', 'what', 'when', 'where', 'who',
            'how', 'it', 'its', 'this', 'that', 'can', 'could', 'please', 'tell', 'me'}
    words = list(dict.fromkeys(word for word in re.findall(r'\w+', text.casefold()) if word not in stop))[:40]
    if not words:
        return []
    match = ' OR '.join('"' + word.replace('"', '""') + '"' for word in words)
    # Permissions and freshness are enforced BEFORE ranking/limiting and before
    # any material is sent to the model. Old legacy two-hour TTLs are capped too.
    rows = connection.execute("""SELECT s.doc_id, f.* FROM knowledge_search s
        LEFT JOIN facts f ON s.doc_id='live:' || f.id
        WHERE knowledge_search MATCH ? AND (s.doc_id LIKE 'static:%' OR
            (f.demo_owner_id IS ? AND f.valid_until>? AND f.created_at>? AND f.created_at<=?))
        ORDER BY bm25(knowledge_search) LIMIT 24""",
        (match, owner, now, now - settings.live_ttl_seconds, now)).fetchall()
    seeds = {fact['id']: fact for fact in static_facts}
    candidates = []
    for row in rows:
        doc_id = row['doc_id']
        if doc_id.startswith('static:'):
            fact = seeds.get(doc_id.removeprefix('static:'))
            if fact:
                candidates.append({**fact, 'id': doc_id, 'kind': 'static', 'answer_type': 'text'})
        else:
            fact = dict(row)
            if not usable_fact(fact, settings, is_usable):
                continue
            # A bare Yes/No is only evidence for the original question, never a
            # paraphrase that might reverse polarity or change date/location.
            if fact['answer_type'] == 'yes_no' and question_identity(text) != question_identity(fact['question']):
                continue
            candidates.append({**fact, 'id': doc_id, 'fact_row_id': fact['id'], 'kind': 'dynamic'})
    # Exact live repeats should not lose their place to broad campus keywords.
    candidates.sort(key=lambda fact: 0 if fact.get('question') and question_identity(fact['question']) == question_identity(text) else 1)
    return candidates[:6]
