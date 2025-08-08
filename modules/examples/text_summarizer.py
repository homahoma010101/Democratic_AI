"""
Example Text Summarizer Module for Democratic AI
Demonstrates how to create a simple AI module using the SDK
"""

import asyncio
from typing import Dict, Any
from democratic_ai import Module, capability, Context


class TextSummarizer(Module):
    """
    A simple text summarization module
    """
    
    def __init__(self):
        super().__init__(
            module_id="text-summarizer",
            name="Text Summarizer",
            version="1.0.0",
            description="Summarizes long text into concise summaries",
            metadata={
                "author": "Democratic AI Team",
                "category": "text-processing",
                "tags": ["nlp", "summarization", "text"]
            }
        )
        
    @capability(
        action="text.summarize",
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 50,
                    "description": "Text to summarize"
                },
                "max_length": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 500,
                    "default": 100,
                    "description": "Maximum length of summary"
                },
                "style": {
                    "type": "string",
                    "enum": ["brief", "detailed", "bullet_points"],
                    "default": "brief",
                    "description": "Summary style"
                }
            },
            "required": ["text"]
        },
        output_schema={
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "original_length": {"type": "integer"},
                "summary_length": {"type": "integer"},
                "compression_ratio": {"type": "number"},
                "key_points": {
                    "type": "array",
                    "items": {"type": "string"}
                }
            }
        },
        description="Summarizes text into a concise version",
        tags=["summarization", "text"],
        cost_per_call=0.1
    )
    async def summarize(
        self, 
        text: str, 
        max_length: int = 100,
        style: str = "brief",
        context: Context = None
    ) -> Dict[str, Any]:
        """
        Summarize text
        
        In a real implementation, this would use an actual AI model
        like GPT, BERT, or a specialized summarization model
        """
        # Simple implementation for demonstration
        sentences = text.split('. ')
        
        if style == "bullet_points":
            # Extract key points
            key_points = sentences[:3] if len(sentences) >= 3 else sentences
            summary = "• " + "\n• ".join(key_points)
        elif style == "detailed":
            # Take first few sentences
            summary = '. '.join(sentences[:5]) + '.'
        else:  # brief
            # Take first sentence and add ellipsis
            summary = sentences[0] + '...' if sentences else text
            
        # Truncate to max_length
        if len(summary) > max_length:
            summary = summary[:max_length-3] + '...'
            
        # Extract key points
        key_points = [s.strip() for s in sentences[:3]] if len(sentences) >= 3 else [text[:50]]
        
        return {
            "summary": summary,
            "original_length": len(text),
            "summary_length": len(summary),
            "compression_ratio": len(summary) / len(text) if text else 0,
            "key_points": key_points
        }
        
    @capability(
        action="text.extract_keywords",
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 10,
                    "description": "Text to extract keywords from"
                },
                "max_keywords": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                    "description": "Maximum number of keywords to extract"
                }
            },
            "required": ["text"]
        },
        output_schema={
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "word": {"type": "string"},
                            "score": {"type": "number"}
                        }
                    }
                }
            }
        },
        description="Extracts key words from text",
        tags=["keywords", "extraction"],
        cost_per_call=0.05
    )
    async def extract_keywords(
        self,
        text: str,
        max_keywords: int = 5,
        context: Context = None
    ) -> Dict[str, Any]:
        """
        Extract keywords from text
        
        In a real implementation, this would use techniques like:
        - TF-IDF
        - RAKE algorithm
        - TextRank
        - BERT-based keyword extraction
        """
        # Simple word frequency implementation for demonstration
        import re
        from collections import Counter
        
        # Simple tokenization
        words = re.findall(r'\b\w+\b', text.lower())
        
        # Remove common stop words
        stop_words = {'the', 'is', 'at', 'which', 'on', 'a', 'an', 'and', 'or', 'but', 'in', 'with', 'to', 'for'}
        words = [w for w in words if w not in stop_words and len(w) > 3]
        
        # Count frequencies
        word_freq = Counter(words)
        
        # Get top keywords
        top_keywords = word_freq.most_common(max_keywords)
        
        # Normalize scores
        max_freq = top_keywords[0][1] if top_keywords else 1
        
        keywords = [
            {
                "word": word,
                "score": freq / max_freq
            }
            for word, freq in top_keywords
        ]
        
        return {"keywords": keywords}
        
    @capability(
        action="text.analyze_sentiment",
        input_schema={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "minLength": 5,
                    "description": "Text to analyze"
                }
            },
            "required": ["text"]
        },
        output_schema={
            "type": "object",
            "properties": {
                "sentiment": {
                    "type": "string",
                    "enum": ["positive", "negative", "neutral"]
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "scores": {
                    "type": "object",
                    "properties": {
                        "positive": {"type": "number"},
                        "negative": {"type": "number"},
                        "neutral": {"type": "number"}
                    }
                }
            }
        },
        description="Analyzes sentiment of text",
        tags=["sentiment", "emotion"],
        cost_per_call=0.05
    )
    async def analyze_sentiment(
        self,
        text: str,
        context: Context = None
    ) -> Dict[str, Any]:
        """
        Analyze sentiment of text
        
        In a real implementation, this would use:
        - Pre-trained sentiment models (BERT, RoBERTa)
        - Lexicon-based approaches
        - Deep learning models
        """
        # Simple keyword-based implementation for demonstration
        positive_words = {'good', 'great', 'excellent', 'amazing', 'wonderful', 'fantastic', 'love', 'best', 'happy'}
        negative_words = {'bad', 'terrible', 'awful', 'horrible', 'hate', 'worst', 'disappointing', 'poor', 'sad'}
        
        text_lower = text.lower()
        
        positive_count = sum(1 for word in positive_words if word in text_lower)
        negative_count = sum(1 for word in negative_words if word in text_lower)
        
        total = positive_count + negative_count
        
        if total == 0:
            sentiment = "neutral"
            confidence = 0.5
            scores = {"positive": 0.33, "negative": 0.33, "neutral": 0.34}
        else:
            positive_score = positive_count / total
            negative_score = negative_count / total
            
            if positive_score > negative_score:
                sentiment = "positive"
                confidence = positive_score
            elif negative_score > positive_score:
                sentiment = "negative"
                confidence = negative_score
            else:
                sentiment = "neutral"
                confidence = 0.5
                
            scores = {
                "positive": positive_score,
                "negative": negative_score,
                "neutral": 1 - (positive_score + negative_score)
            }
            
        return {
            "sentiment": sentiment,
            "confidence": confidence,
            "scores": scores
        }


async def main():
    """
    Main function to run the text summarizer module
    """
    # Create module instance
    module = TextSummarizer()
    
    # Print module info
    print(f"Starting {module.name} v{module.version}")
    print(f"Module ID: {module.module_id}")
    print(f"Capabilities: {[cap.action for cap in module.capabilities]}")
    
    # Connect to hub
    hub_url = "ws://localhost:8765"  # Default hub URL
    
    try:
        await module.run(hub_url)
    except KeyboardInterrupt:
        print("\nShutting down module...")
    except Exception as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    asyncio.run(main())
