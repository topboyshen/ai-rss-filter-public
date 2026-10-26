"""
User feedback routes for improving scoring accuracy
"""
from flask import Blueprint, request, jsonify
from routes.shared import check_api_key
import logging

feedback_bp = Blueprint('feedback', __name__, url_prefix='/api/feedback')
logger = logging.getLogger(__name__)

@feedback_bp.route('/score', methods=['POST'])
@check_api_key
def submit_score_feedback():
    """User corrects a score"""
    data = request.json
    entry_id = data.get('entry_id')
    corrected_score = data.get('corrected_score')
    reason = data.get('reason', '')
    
    # Store feedback in database
    # TODO: Implement database storage
    
    logger.info(f'Feedback received: entry_id={entry_id}, score={corrected_score}, reason={reason}')
    
    return jsonify({'status': 'success', 'message': 'Feedback recorded'})

@feedback_bp.route('/tag', methods=['POST'])
@check_api_key
def submit_tag_feedback():
    """User corrects a tag"""
    data = request.json
    entry_id = data.get('entry_id')
    corrected_tag = data.get('corrected_tag')
    
    logger.info(f'Tag feedback: entry_id={entry_id}, tag={corrected_tag}')
    
    return jsonify({'status': 'success', 'message': 'Tag feedback recorded'})
