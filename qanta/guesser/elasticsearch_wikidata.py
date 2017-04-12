import pickle
import os
from itertools import repeat
from typing import List, Optional, Dict, Set
from qanta.spark_execution import create_spark_context

import elasticsearch
from elasticsearch_dsl import DocType, Text, Keyword, Search, Index, Boolean
import progressbar
from sklearn.linear_model import RidgeClassifier
from sklearn.pipeline import Pipeline
from sklearn.feature_extraction.text import TfidfVectorizer

from qanta.datasets.abstract import QuestionText
from qanta.datasets.quiz_bowl import QuizBowlDataset
from qanta.guesser.abstract import AbstractGuesser
from qanta.preprocess import format_guess
from qanta.config import conf
from qanta.util.io import safe_open
from qanta import logging
from qanta.wikipedia.cached_wikipedia import CachedWikipedia


log = logging.get(__name__)

IS_HUMAN_MODEL_PICKLE = 'is_human_model.pickle'


class Answer(DocType):
    page = Text(fields={'raw': Keyword()})
    wiki_content = Text()
    qb_content = Text()
    is_human = Boolean()

    class Meta:
        index = 'qb'


class ElasticSearchIndex:
    @staticmethod
    def build(documents: Dict[str, str], is_human_map):
        try:
            Index('qb').delete()
        except elasticsearch.exceptions.NotFoundError:
            log.info('Could not delete non-existent index, creating new index...')
        Answer.init()
        cw = CachedWikipedia()
        bar = progressbar.ProgressBar()
        for page in bar(documents):
            answer = Answer(
                page=page,
                wiki_content=cw[page].content,
                qb_content=documents[page],
                is_human=is_human_map[page]
            )
            answer.save()

    def search(self, text: str, is_human: bool):
        s = Search(index='qb')\
            .filter('term', is_human=is_human)\
            .query(
                'multi_match',
                query=text,
                fields=['wiki_content', 'qb_content']
            )
        results = s.execute()
        return [(r.page, r.meta.score) for r in results]


es_index = ElasticSearchIndex()


def create_instance_of_map(formatted_answers: Set[str]):
    with open('output/wikidata.pickle', 'rb') as f:
        d = pickle.load(f)
        parsed_item_map = d['parsed_item_map']
        instance_of_map = {}
        for page, properties in parsed_item_map.items():
            guess = format_guess(page)
            if 'instance of' in properties and guess in formatted_answers and len(properties['instance of']) > 0:
                instance_of_map[guess] = set(properties['instance of'])
        return instance_of_map


def create_is_human_map(instance_of_map):
    is_human_map = {}
    for p in instance_of_map:
        if 'Human' in instance_of_map[p]:
            is_human_map[p] = True
        else:
            is_human_map[p] = False
    return is_human_map


def format_human_data(is_human_map, questions: List[List[str]], pages: List[str]):
    x_data = []
    y_data = []

    for q, p in zip(questions, pages):
        full_text = ' '.join(q)
        x_data.append(full_text)

        p = format_guess(p)
        y_data.append(int(is_human_map[p]))

    return x_data, y_data


def create_human_model(x_data, y_data):
    pipeline = Pipeline([
        ('tfidf', TfidfVectorizer(ngram_range=(1, 2), min_df=2)),
        ('ridge', RidgeClassifier())
    ])
    return pipeline.fit(x_data, y_data)


class ElasticSearchWikidataGuesser(AbstractGuesser):
    def __init__(self, is_human_model=None):
        super().__init__()
        self.is_human_model = is_human_model

    def qb_dataset(self):
        return QuizBowlDataset(conf['guessers']['ElasticSearch']['min_appearances'])

    def train(self, training_data):
        answers = {format_guess(a) for a in training_data[1]}
        log.info('Training is_human model...')
        instance_of_map = create_instance_of_map(answers)
        is_human_map = create_is_human_map(instance_of_map)
        x_data, y_data = format_human_data(is_human_map, training_data[0], training_data[1])
        self.is_human_model = create_human_model(x_data, y_data)

        log.info('Building Elastic Search Index...')
        documents = {}
        for sentences, ans in zip(training_data[0], training_data[1]):
            page = format_guess(ans)
            paragraph = ' '.join(sentences)
            if page in documents:
                documents[page] += ' ' + paragraph
            else:
                documents[page] = paragraph
        ElasticSearchIndex.build(documents, is_human_map)

    def guess(self,
              questions: List[QuestionText],
              max_n_guesses: Optional[int]):
        n_cores = conf['guessers']['ElasticSearch']['n_cores']
        sc = create_spark_context([('spark.executor.cores', n_cores)])
        b_is_human_model = sc.broadcast(self.is_human_model)

        def ir_search(query):
            is_human_model = b_is_human_model.value
            is_human = bool(is_human_model.transform([query])[0])
            return es_index.search(query, is_human)[:max_n_guesses]

        return sc.parallelize(questions).map(ir_search).collect()

    @classmethod
    def targets(cls):
        return [IS_HUMAN_MODEL_PICKLE]

    @classmethod
    def load(cls, directory: str):
        with open(os.path.join(directory, IS_HUMAN_MODEL_PICKLE), 'rb') as f:
            is_human_model = pickle.load(f)['is_human_model']
        return ElasticSearchWikidataGuesser(is_human_model=is_human_model)

    def save(self, directory: str):
        with safe_open(os.path.join(directory, IS_HUMAN_MODEL_PICKLE), 'wb') as f:
            pickle.dump({'is_human_model': self.is_human_model}, f)
