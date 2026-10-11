import json
import os
from pathlib import Path
from urllib import error, request as urlrequest

from flask import Blueprint, current_app, jsonify, request
from src.models.note import Note, db

note_bp = Blueprint('note', __name__)

TRANSLATION_LANGUAGES = {
    'zh-CN': 'Simplified Chinese',
    'zh-TW': 'Traditional Chinese',
    'ja': 'Japanese',
    'en': 'English',
    'ko': 'Korean',
    'fr': 'French',
    'de': 'German',
    'es': 'Spanish',
}
TRANSLATION_PROMPT_PATH = Path(__file__).resolve().parents[2] / 'prompt' / 'translate.txt'
OPENROUTER_URL = 'https://openrouter.ai/api/v1/chat/completions'


def _escape_unescaped_json_control_characters(text):
    """Escape literal control characters inside JSON string values."""
    escaped_text = []
    in_string = False
    escaped = False

    for character in text:
        if in_string:
            if escaped:
                escaped_text.append(character)
                escaped = False
            elif character == '\\':
                escaped_text.append(character)
                escaped = True
            elif character == '"':
                escaped_text.append(character)
                in_string = False
            elif ord(character) < 0x20:
                escaped_text.append(f'\\u{ord(character):04x}')
            else:
                escaped_text.append(character)
        else:
            escaped_text.append(character)
            if character == '"':
                in_string = True

    return ''.join(escaped_text)


@note_bp.route('/translate', methods=['POST'])
def translate_note():
    """Translate an unsaved note draft using the configured OpenRouter model."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'A JSON object is required'}), 400

    target_language = data.get('target_language')
    if target_language not in TRANSLATION_LANGUAGES:
        return jsonify({'error': 'Choose a supported translation language'}), 400

    title = data.get('title', '')
    content = data.get('content', '')
    if not isinstance(title, str) or not isinstance(content, str):
        return jsonify({'error': 'Title and content must be text'}), 400
    if not title.strip() and not content.strip():
        return jsonify({'error': 'Enter a title or content to translate'}), 400

    api_key = os.getenv('OPENROUTER_API_KEY')
    if not api_key:
        return jsonify({'error': 'OpenRouter is not configured. Set OPENROUTER_API_KEY in the root .env file.'}), 503

    try:
        prompt_template = TRANSLATION_PROMPT_PATH.read_text(encoding='utf-8')
    except OSError:
        current_app.logger.exception('Unable to read translation prompt at %s', TRANSLATION_PROMPT_PATH)
        return jsonify({'error': 'The translation prompt file could not be read'}), 500

    system_prompt = prompt_template.replace(
        '{{target_language}}',
        TRANSLATION_LANGUAGES[target_language],
    )
    payload = {
        'model': os.getenv('OPENROUTER_MODEL', 'openai/gpt-4o-mini'),
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {
                'role': 'user',
                'content': json.dumps(
                    {'title': title, 'content': content},
                    ensure_ascii=False,
                ),
            },
        ],
    }
    outbound_request = urlrequest.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
            'HTTP-Referer': 'http://localhost:5001',
            'X-Title': 'NoteTaker',
        },
        method='POST',
    )

    try:
        with urlrequest.urlopen(outbound_request, timeout=60) as response:
            provider_response = json.loads(response.read().decode('utf-8'))
    except error.HTTPError as exc:
        error_body = exc.read().decode('utf-8', errors='replace')
        current_app.logger.warning('OpenRouter returned HTTP %s: %s', exc.code, error_body)
        try:
            provider_error = json.loads(error_body).get('error', {}).get('message')
        except (json.JSONDecodeError, AttributeError):
            provider_error = None
        message = provider_error or f'OpenRouter request failed with HTTP {exc.code}'
        if exc.code == 429:
            response = jsonify({'error': message})
            retry_after = exc.headers.get('Retry-After') if exc.headers else None
            if retry_after:
                response.headers['Retry-After'] = retry_after
            return response, 429
        return jsonify({'error': message}), 502
    except (error.URLError, TimeoutError) as exc:
        current_app.logger.warning('OpenRouter request failed: %s', exc)
        return jsonify({'error': 'Could not reach OpenRouter. Check your connection and try again.'}), 502
    except (json.JSONDecodeError, UnicodeDecodeError):
        current_app.logger.exception('OpenRouter returned an invalid response')
        return jsonify({'error': 'OpenRouter returned an invalid response'}), 502

    try:
        translated_text = provider_response['choices'][0]['message']['content']
        translated_text = _escape_unescaped_json_control_characters(translated_text)
        translated = json.loads(translated_text)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
        current_app.logger.exception('OpenRouter response did not contain a valid translation')
        return jsonify({'error': 'The translation response was not valid JSON'}), 502

    if (
        not isinstance(translated, dict)
        or not isinstance(translated.get('title'), str)
        or not isinstance(translated.get('content'), str)
    ):
        return jsonify({'error': 'The translation response must contain title and content text'}), 502

    translated['title'] = translated['title'].replace('\\n', '\n')
    translated['content'] = translated['content'].replace('\\n', '\n')

    return jsonify({
        'title': translated['title'],
        'content': translated['content'],
        'llm_response': translated_text,
    })


@note_bp.route('/notes', methods=['GET'])
def get_notes():
    """Get all notes, ordered by most recently updated"""
    notes = Note.query.order_by(Note.updated_at.desc()).all()
    return jsonify([note.to_dict() for note in notes])

@note_bp.route('/notes', methods=['POST'])
def create_note():
    """Create a new note"""
    try:
        data = request.json
        if not data or 'title' not in data or 'content' not in data:
            return jsonify({'error': 'Title and content are required'}), 400
        
        note = Note(title=data['title'], content=data['content'])
        db.session.add(note)
        db.session.commit()
        return jsonify(note.to_dict()), 201
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@note_bp.route('/notes/<int:note_id>', methods=['GET'])
def get_note(note_id):
    """Get a specific note by ID"""
    note = Note.query.get_or_404(note_id)
    return jsonify(note.to_dict())

@note_bp.route('/notes/<int:note_id>', methods=['PUT'])
def update_note(note_id):
    """Update a specific note"""
    try:
        note = Note.query.get_or_404(note_id)
        data = request.json
        
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        note.title = data.get('title', note.title)
        note.content = data.get('content', note.content)
        db.session.commit()
        return jsonify(note.to_dict())
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@note_bp.route('/notes/<int:note_id>', methods=['DELETE'])
def delete_note(note_id):
    """Delete a specific note"""
    try:
        note = Note.query.get_or_404(note_id)
        db.session.delete(note)
        db.session.commit()
        return '', 204
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

@note_bp.route('/notes/search', methods=['GET'])
def search_notes():
    """Search notes by title or content"""
    query = request.args.get('q', '')
    if not query:
        return jsonify([])
    
    notes = Note.query.filter(
        (Note.title.contains(query)) | (Note.content.contains(query))
    ).order_by(Note.updated_at.desc()).all()
    
    return jsonify([note.to_dict() for note in notes])
