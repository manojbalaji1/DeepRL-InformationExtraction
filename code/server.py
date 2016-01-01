import zmq, time
import numpy as np
import copy
import sys, json, pdb, pickle, operator, collections
import helper
import predict2 as predict
from train import load_data
from itertools import izip
import inflect
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import TfidfVectorizer
import argparse
from random import shuffle
from operator import itemgetter
import matplotlib.pyplot as plt


DEBUG = False

#Global variables
int2tags = ['shooterName','numKilled', 'numWounded', 'city']
NUM_ENTITIES = len(int2tags)
WORD_LIMIT = 1000
STATE_SIZE = 4*NUM_ENTITIES+1

trained_model = None
tfidf_vectorizer = TfidfVectorizer()
inflect_engine = inflect.engine()

def dd():
    return {}

#global caching to speed up
TRAIN_TFIDF_MATRICES = {}
TRAIN_ENTITIES = collections.defaultdict(dd)
TRAIN_CONFIDENCES = collections.defaultdict(dd)

TEST_TFIDF_MATRICES = {}
TEST_ENTITIES = collections.defaultdict(dd)
TEST_CONFIDENCES = collections.defaultdict(dd)

CORRECT = collections.defaultdict(lambda:0.)
GOLD = collections.defaultdict(lambda:0.)
PRED = collections.defaultdict(lambda:0.)
EVALCONF = collections.defaultdict(lambda:[])
EVALCONF2 = collections.defaultdict(lambda:[])
evalMode = False

#Environment for each episode
class Environment:
    def __init__(self, originalArticle, newArticles, goldEntities, indx, args, evalMode):
        self.indx = indx
        self.originalArticle = originalArticle
        self.newArticles = newArticles #extra articles to process
        self.goldEntities = goldEntities 
        self.ignoreDuplicates = args.ignoreDuplicates        
        self.entity = args.entity
        self.aggregate = args.aggregate
        self.delayedReward = args.delayedReward

        self.shuffledIndxs = range(len(self.newArticles))
        if not evalMode and args.shuffleArticles:
            shuffle(self.shuffledIndxs) #IMP: remove this to eliminate shuffling
       
        self.state = [0 for i in range(STATE_SIZE)]
        self.terminal = False
        
        self.bestEntities = collections.defaultdict(lambda:'') #current best entities
        self.bestConfidences = collections.defaultdict(lambda:0.)
        self.bestEntitySet = None
        if self.aggregate == 'majority':
            self.bestEntitySet = collections.defaultdict(lambda:[])

        # to keep track of extracted values from previousArticle
        if 0 in ENTITIES[self.indx]:
            self.prevEntities, self.prevConfidences = ENTITIES[self.indx][0], CONFIDENCES[self.indx][0]
        else:
            self.prevEntities, self.prevConfidences = self.extractEntitiesWithConfidences(self.originalArticle)
            ENTITIES[self.indx][0] = self.prevEntities
            CONFIDENCES[self.indx][0] = self.prevConfidences

        #calculate tf-idf similarities
        self.allArticles = [originalArticle] + self.newArticles
        self.allArticles = [' '.join(q) for q in self.allArticles]        

        if self.indx in TFIDF_MATRICES:
            self.tfidf_matrix = TFIDF_MATRICES[self.indx]
        else:
            self.tfidf_matrix = tfidf_vectorizer.fit_transform(self.allArticles)
            TFIDF_MATRICES[self.indx] = self.tfidf_matrix

        #update the initial state
        self.stepNum = 0
        self.updateState(1, self.ignoreDuplicates)

        #store the original entities
        self.originalEntities = self.prevEntities
        
        return
    
    def extractEntitiesWithConfidences(self, article):
        #article is a list of words
        joined_article = ' '.join(article)
        pred, conf_scores, conf_cnts = predict.predictWithConfidences(trained_model, joined_article, False, helper.cities)

        for i in range(len(conf_scores)):
            if conf_cnts[i] > 0:
                conf_scores[i] /= conf_cnts[i]

        return pred.split(','), conf_scores

    #find the article similarity between original and newArticle[i] (=allArticles[i+1])
    def articleSim(self, i):
        return cosine_similarity(self.tfidf_matrix[0:1], self.tfidf_matrix[i+1:i+2])[0][0]

    # update the state based on the decision from DQN
    def updateState(self, action, ignoreDuplicates=False):
        articleIndx = None
        if ignoreDuplicates:
            nextArticle = None
            while not nextArticle and self.stepNum < len(self.newArticles):
                articleIndx = self.shuffledIndxs[self.stepNum]
                if self.articleSim(articleIndx) < 0.95:
                    nextArticle =self.newArticles[articleIndx]
                else:
                    self.stepNum += 1                
        else:
            #get next article
            if self.stepNum < len(self.newArticles):
                articleIndx = self.shuffledIndxs[self.stepNum]
                nextArticle = self.newArticles[articleIndx]
            else:
                nextArticle = None

        if action == 1:
            # integrate the values into the current DB state
            entities, confidences = self.prevEntities, self.prevConfidences           

            # all other tags
            for i in range(NUM_ENTITIES):
                if self.aggregate == 'majority':
                    self.bestEntitySet[i].append((entities[i], confidences[i]))
                    self.bestEntities[i], self.bestConfidences[i] = self.majorityVote(self.bestEntitySet[i])
                else:
                    if i==0:
                        #handle shooterName -  add to list
                        if not self.bestEntities[i]:
                            self.bestEntities[i] = entities[i]
                            self.bestConfidences[i] = confidences[i]                        
                        elif self.aggregate == 'always' or confidences[i] > self.bestConfidences[i]:
                            self.bestEntities[i] = entities[i]
                            # self.bestEntities[i] = self.bestEntities[i] + '|' + entities[i]
                            self.bestConfidences[i] = confidences[i]                        
                    else:
                        if not self.bestEntities[i] or self.aggregate == 'always' or confidences[i] > self.bestConfidences[i]:
                            self.bestEntities[i] = entities[i]
                            self.bestConfidences[i] = confidences[i]
                            # print "Changing best Entities"
                            # print "New entities", self.bestEntities
            if DEBUG:
                print "entitySet:", self.bestEntitySet

        if nextArticle:               
            assert(articleIndx != None)
            if (articleIndx+1) in ENTITIES[self.indx]:
                entities, confidences = ENTITIES[self.indx][articleIndx+1], CONFIDENCES[self.indx][articleIndx+1]
            else:
                entities, confidences = self.extractEntitiesWithConfidences(nextArticle)
                ENTITIES[self.indx][articleIndx+1], CONFIDENCES[self.indx][articleIndx+1] = entities, confidences
            assert(len(entities) == len(confidences))          
        else:
            # print "No next article"
            entities, confidences = [""]*NUM_ENTITIES, [0]*NUM_ENTITIES
            self.terminal = True

        #modify self.state appropriately        
        # print(self.bestEntities, entities)
        matches = map(self.checkEquality, self.bestEntities.values()[1:-1], entities[1:-1])
        matches.insert(0, self.checkEqualityShooter(self.bestEntities.values()[0], entities[0]))
        matches.append(self.checkEqualityCity(self.bestEntities.values()[-1], entities[-1]))
        # pdb.set_trace()
        self.state = [0 for i in range(STATE_SIZE)]
        for i in range(NUM_ENTITIES):
            self.state[i] = self.bestConfidences[i] #DB state
            self.state[NUM_ENTITIES+i] = confidences[i]  #IMP: (original) next article state            
            matchScore = float(matches[i])
            if matchScore > 0:
                self.state[2*NUM_ENTITIES+i] = 1
            else:
                self.state[3*NUM_ENTITIES+i] = 1

            # self.state[2*NUM_ENTITIES+i] = float(matches[i])*confidences[i] if float(matches[i])>0 else -1*confidences[i]
            if nextArticle:
                self.state[-1] = self.articleSim(articleIndx)
            else:
                self.state[-1] = 0

        #selectively mask states
        if self.entity != 4:
            for j in range(NUM_ENTITIES):
                if j != self.entity:
                    self.state[j] = 0
                    self.state[NUM_ENTITIES+j] = 0            


        #update state variables
        self.prevEntities = entities
        self.prevConfidences = confidences

        return

    # check if two entities are equal. Need to handle city
    def checkEquality(self, e1, e2):         
        # if gold is unknown, then dont count that
        return e2!=''  and e1.lower() == e2.lower()

    def checkEqualityShooter(self, e1, e2):
        if e2 == '': return 0.

        gold = set(e2.lower().split('|'))
        pred = set(e1.lower().split('|'))
        correct = len(gold.intersection(pred))
        prec = float(correct)/len(pred)
        rec = float(correct)/len(gold)
        if prec+rec > 0:
            f1 = (2*prec*rec)/(prec+rec)
        else:
            f1 = 0.
        return f1

    def checkEqualityCity(self, e1, e2):
        if e2!='' and e1!='':
            gold = set(e2.lower().split())
            pred = set(e1.lower().split())
            correct = len(gold.intersection(pred))
            prec = float(correct)/len(pred)
            rec = float(correct)/len(gold)
            if prec+rec > 0:
                f1 = (2*prec*rec)/(prec+rec)
            else:
                f1 = 0.
            return f1

        return 0
        

    def calculateReward(self, oldEntities, newEntities):
        rewards = [int(self.checkEquality(newEntities[1], self.goldEntities[1])) - int(self.checkEquality(oldEntities[1], self.goldEntities[1])),
                    int(self.checkEquality(newEntities[2], self.goldEntities[2])) - int(self.checkEquality(oldEntities[2], self.goldEntities[2]))]


        #add in shooter reward
        if self.goldEntities[0]:
            rewards.insert(0, self.checkEqualityShooter(newEntities[0], self.goldEntities[0]) \
                    - self.checkEqualityShooter(oldEntities[0], self.goldEntities[0]))
        else:
            rewards.insert(0, 0.)

        rewards.append(self.checkEqualityCity(newEntities[-1], self.goldEntities[-1]) \
                - self.checkEqualityCity(oldEntities[-1], self.goldEntities[-1]))

        # if shooter_reward != 0:
        #     print "Shooter reward", shooter_reward

        if DEBUG:
            #print
            print "oldEntities", oldEntities
            print "newEntities:", newEntities
            print "goldEntities:", self.goldEntities
            print "matches:", sum(map(self.checkEquality, newEntities[1:], self.goldEntities[1:]))
            print rewards            

        #TODO: if terminal, give some reward based on how many entities are correct?

        # pdb.set_trace()

        if self.entity == 4:
            return sum(rewards)
        else:
            return rewards[self.entity]

        

    #evaluate the bestEntities retrieved so far for a single article
    #IMP: make sure the evaluate variables are properly re-initialized
    def evaluateArticle(self, predEntities, goldEntities, shooterLenientEval, shooterLastName, evalOutFile):
        # print "Evaluating article", predEntities, goldEntities

        #shooterName first: only add this if gold contains a valid shooter
        if goldEntities[0]!='':
            if shooterLastName:
                gold = set(goldEntities[0].lower().split('|')[-1:])
            else:
                gold = set(goldEntities[0].lower().split('|'))

            pred = set(predEntities[0].lower().split('|'))
            correct = len(gold.intersection(pred))

            # print "Gold:",goldEntities
            # print "Pred:",predEntities
            # print gold, pred, correct
            # pdb.set_trace()

            if shooterLenientEval:
                CORRECT[int2tags[0]] += (1 if correct> 0 else 0)
                GOLD[int2tags[0]] += (1 if len(gold) > 0 else 0)
                PRED[int2tags[0]] += (1 if len(pred) > 0 else 0)            
            else:
                CORRECT[int2tags[0]] += correct
                GOLD[int2tags[0]] += len(gold)
                PRED[int2tags[0]] += len(pred)

            # FOR DEBUGGING
            # if correct != len(gold):
            #     print "Gold:", gold
            #     print "Pred:", pred
            #     #print all articles
            #     for i in range(len(self.allArticles)):
            #         print self.allArticles[i]
            #         print "----------------------------"
            #     pdb.set_trace()


        #all other tags
        for i in range(1, NUM_ENTITIES):   
            gold = set(goldEntities[i].lower().split())
            pred = set(predEntities[i].lower().split())
            correct = len(gold.intersection(pred))      
            GOLD[int2tags[i]] += len(gold)
            PRED[int2tags[i]] += len(pred)
            # if predEntities[i].lower() == goldEntities[i].lower():
            CORRECT[int2tags[i]] += correct

        if evalOutFile:
            evalOutFile.write("--------------------\n")
            evalOutFile.write("Gold: "+str(gold)+"\n")
            evalOutFile.write("Pred: "+str(pred)+"\n")
            evalOutFile.write("Correct: "+str(correct)+"\n")



    def oracleEvaluate(self, goldEntities, entityDic, confDic):
        # the best possible numbers assuming that just the right information is extracted 
        # from the set of related articles
        global PRED, GOLD, CORRECT, EVALCONF, EVALCONF2
        bestPred, bestCorrect = collections.defaultdict(lambda:0.), collections.defaultdict(lambda:0.)
        bestConf = collections.defaultdict(lambda:0.)

        for stepNum, predEntities in entityDic.items():   

            #shooterName first: only add this if gold contains a valid shooter
            if goldEntities[0]!='':
                gold = set(goldEntities[0].lower().split('|'))

                pred = set(predEntities[0].lower().split('|'))
                correct = len(gold.intersection(pred))

                if correct > bestCorrect[int2tags[0]] or (correct == bestCorrect[int2tags[0]] and len(pred) < bestPred[int2tags[0]]):
                    # print "Correct: ", correct
                    # print "Gold:", gold
                    # print "pred:", pred
                    bestCorrect[int2tags[0]] = correct
                    bestPred[int2tags[0]] = len(pred)
                    bestConf[int2tags[0]] = confDic[stepNum][0]

                if stepNum == 0:
                    GOLD[int2tags[0]] += len(gold)

                if correct==0:
                    EVALCONF2[int2tags[0]].append(confDic[stepNum][0])


            #all other tags
            for i in range(1, NUM_ENTITIES): 
                if goldEntities[i].lower() == 'zero': continue  
                gold = set(goldEntities[i].lower().split())
                pred = set(predEntities[i].lower().split())
                correct = len(gold.intersection(pred))      
                if correct > bestCorrect[int2tags[i]] or (correct == bestCorrect[int2tags[i]] and len(pred) < bestPred[int2tags[i]]):
                    bestCorrect[int2tags[i]] = correct
                    bestPred[int2tags[i]] = len(pred)
                    bestConf[int2tags[i]] = confDic[stepNum][i]
                    # print "Correct: ", correct
                    # print "Gold:", gold
                    # print "pred:", pred
                if stepNum == 0:
                    GOLD[int2tags[i]] += len(gold)
                if correct==0:
                    EVALCONF2[int2tags[i]].append(confDic[stepNum][i])

        for i in range(NUM_ENTITIES):    
            PRED[int2tags[i]] += bestPred[int2tags[i]]
            CORRECT[int2tags[i]] += bestCorrect[int2tags[i]]     
            EVALCONF[int2tags[i]].append(bestConf[int2tags[i]])   


    def thresholdEvaluate(self, goldEntities, thres=0.0):
        # the best possible numbers assuming that just the right information is extracted 
        # from the set of related articles
        global PRED, GOLD, CORRECT, EVALCONF, EVALCONF2
        global ENTITIES, CONFIDENCES
        bestPred, bestCorrect = collections.defaultdict(lambda:0.), collections.defaultdict(lambda:0.)
        bestConf = collections.defaultdict(lambda:0.)
        bestSim = 0.
        bestEntities = ['','','','']
        aggEntites = collections.defaultdict(lambda:collections.defaultdict(lambda:0.))       

        #add in the original entities
        for i in range(NUM_ENTITIES):
            aggEntites[i][self.bestEntities[i]] += 1.1

        for i in range(len(self.newArticles)):

            sim = self.articleSim(i)
            # print sim

            # if sim > bestSim:
            #     bestSim = sim
            #     entities, confidences = self.extractEntitiesWithConfidences(self.newArticles[i])
            #     bestEntities = entities
            #     bestConf = confidences
            #     print bestSim
            if sim > thres:
                if (i+1) in ENTITIES[self.indx]:
                    entities, confidences = ENTITIES[self.indx][i+1], CONFIDENCES[self.indx][i+1]
                else:
                    entities, confidences = self.extractEntitiesWithConfidences(self.newArticles[i])
                for j in range(NUM_ENTITIES):
                    if entities[j]:
                        aggEntites[j][entities[j]] += 1


        #choose the best entities now
        for i in range(NUM_ENTITIES):
            tmp = sorted(aggEntites[i].items(), key=itemgetter(1), reverse=True)
            bestEntities[i] = tmp[0][0]

            print i, tmp
            # pdb.set_trace()


        self.evaluateArticle(bestEntities, goldEntities, False, False, False)

    #TODO: use conf or 1 for mode calculation
    def majorityVote(self, entityList):
        if not entityList: return '',0.

        dic = collections.defaultdict(lambda:0.)
        confDic = collections.defaultdict(lambda:0.)
        cnt = collections.defaultdict(lambda:0.)
        ticker = 0
        for entity, conf in entityList:
            dic[entity] += 1
            cnt[entity] += 1
            confDic[entity] += conf
            if ticker == 0: dic[entity] += 0.1 #extra for original article
            ticker += 1

        bestEntity, bestConf = sorted(dic.items(), key=itemgetter(1), reverse=True)[0]
        return bestEntity, confDic[bestEntity]/cnt[bestEntity]


    #take a single step in the episode
    def step(self, action):
        oldEntities = copy.copy(self.bestEntities.values())

        #update pointer to next article
        self.stepNum += 1
    
        self.updateState(action, self.ignoreDuplicates)

        newEntities = self.bestEntities.values()
    
        if self.delayedReward == 'True':
            reward = self.calculateReward(self.originalEntities, newEntities)
        else:
            reward = self.calculateReward(oldEntities, newEntities)

        return self.state, reward, self.terminal


def loadFile(filename):
    articles, titles, identifiers, downloaded_articles = [], [] ,[] ,[]

    #load data and process identifiers
    with open(filename, "rb") as inFile:
        while True:
            try:
                a, b, c, d = pickle.load(inFile)
                articles.append(a)
                titles.append(b)
                identifiers.append(c)
                downloaded_articles.append(d)             
            except:
                break

    identifiers_tmp = []  
    for e in identifiers:        
        for i in range(NUM_ENTITIES):
            if type(e[i]) == int or e[i].isdigit():            
                e[i] = int(e[i])
                e[i] = inflect_engine.number_to_words(e[i])            
        identifiers_tmp.append(e)
    identifiers = identifiers_tmp

    return articles, titles, identifiers, downloaded_articles

def baselineEval(articles, identifiers, args):
    global CORRECT, GOLD, PRED
    CORRECT = collections.defaultdict(lambda:0.)
    GOLD = collections.defaultdict(lambda:0.)
    PRED = collections.defaultdict(lambda:0.)
    for indx in range(len(articles)):
        print "INDX:", indx        
        originalArticle = articles[indx][0]
        newArticles = []
        goldEntities = identifiers[indx]        
        env = Environment(originalArticle, newArticles, goldEntities, indx, args, False)
        env.evaluateArticle(env.bestEntities.values(), env.goldEntities, args.shooterLenientEval, args.shooterLastName, args.evalOutFile)

    print "------------\nEvaluation Stats: (Precision, Recall, F1):"
    for tag in int2tags:
        prec = CORRECT[tag]/PRED[tag]
        rec = CORRECT[tag]/GOLD[tag]
        f1 = (2*prec*rec)/(prec+rec)
        print tag, prec, rec, f1, "########", CORRECT[tag], PRED[tag], GOLD[tag]


def thresholdEval(articles, downloaded_articles, identifiers, args):
    global CORRECT, GOLD, PRED
    THRES = 0.6
    CORRECT = collections.defaultdict(lambda:0.)
    GOLD = collections.defaultdict(lambda:0.)
    PRED = collections.defaultdict(lambda:0.)
    for indx in range(len(articles)):
        print "INDX:", indx        
        originalArticle = articles[indx][0]
        newArticles = [q.split(' ')[:WORD_LIMIT] for q in downloaded_articles[indx]]
        goldEntities = identifiers[indx]   
        env = Environment(originalArticle, newArticles, goldEntities, indx, args, False)
        env.thresholdEvaluate(env.goldEntities, THRES)

    print "------------\nEvaluation Stats: (Precision, Recall, F1):"
    for tag in int2tags:
        prec = CORRECT[tag]/PRED[tag]
        rec = CORRECT[tag]/GOLD[tag]
        f1 = (2*prec*rec)/(prec+rec)
        print tag, prec, rec, f1

def plot_hist(evalconf, name):
    for i in evalconf.keys():
        plt.hist(evalconf[i], bins=[0, 0.1,0.2,0.3,0.4,0.5,0.6,0.7,0.8,0.9,1.0])
        plt.title("Gaussian Histogram")
        plt.xlabel("Value")
        plt.ylabel("Frequency")
        # plt.show()
        plt.savefig(name+"_"+str(i)+".png")
        plt.clf()

def main(args):
    global ENTITIES, CONFIDENCES, TFIDF_MATRICES
    global TRAIN_ENTITIES, TRAIN_CONFIDENCES, TRAIN_TFIDF_MATRICES
    global TEST_ENTITIES, TEST_CONFIDENCES, TEST_TFIDF_MATRICES
    global evalMode
    global CORRECT, GOLD, PRED, EVALCONF, EVALCONF2
    global trained_model
    
    print args

    trained_model = pickle.load( open(args.modelFile, "rb" ) )

    #load cached entities (speed up)
    if args.trainEntities:
        TRAIN_ENTITIES, TRAIN_CONFIDENCES, TRAIN_TFIDF_MATRICES = pickle.load(open(args.trainEntities, "rb"))

    if args.testEntities:
        TEST_ENTITIES, TEST_CONFIDENCES, TEST_TFIDF_MATRICES = pickle.load(open(args.testEntities, "rb"))

    assert(not (args.testEntities and not args.testFile))

    train_articles, train_titles, train_identifiers, train_downloaded_articles = loadFile(args.trainFile)
    if args.testFile:
        test_articles, test_titles, test_identifiers, test_downloaded_articles = loadFile(args.testFile)
    else:
        print "Using trainFile for eval"        
        TEST_ENTITIES = TRAIN_ENTITIES
        TEST_CONFIDENCES = TRAIN_CONFIDENCES
        TEST_TFIDF_MATRICES = TRAIN_TFIDF_MATRICES
        test_articles, test_titles, test_identifiers, test_downloaded_articles = train_articles, train_titles, train_identifiers, train_downloaded_articles
 
    #starting assignments
    ENTITIES = TRAIN_ENTITIES
    CONFIDENCES = TRAIN_CONFIDENCES
    TFIDF_MATRICES = TRAIN_TFIDF_MATRICES
    articles, titles, identifiers, downloaded_articles = train_articles, train_titles, train_identifiers, train_downloaded_articles

    if args.baselineEval:
        baselineEval(articles, identifiers, args)
        return
    elif args.thresholdEval:
        thresholdEval(articles, downloaded_articles, identifiers, args)
        return
    

    articleNum = 0
    savedArticleNum = 0

    outFile = open(args.outFile, 'w', 0) #unbuffered
    outFile.write(str(args)+"\n")

    evalOutFile = None
    if args.evalOutFile != '':
        evalOutFile = open(args.evalOutFile, 'w')

    # pdb.set_trace()

    #server setup
    port = args.port
    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind("tcp://*:%s" % port)
    print "Started server on port", port

    # server loop
    while True:
        #  Wait for next request from client
        message = socket.recv()
        # print "Received request: ", message

        if message == "newGame":
            # indx = articleNum % 10 #for test
            indx = articleNum % len(articles)
            if DEBUG: print "INDX:", indx
            articleNum += 1
            originalArticle = articles[indx][0]
            newArticles = [q.split(' ')[:WORD_LIMIT] for q in downloaded_articles[indx]]
            goldEntities = identifiers[indx]   
            env = Environment(originalArticle, newArticles, goldEntities, indx, args, evalMode)
            newstate, reward, terminal = env.state, 0, 'false'

        elif message == "evalStart":
            CORRECT = collections.defaultdict(lambda:0.)
            GOLD = collections.defaultdict(lambda:0.)
            PRED = collections.defaultdict(lambda:0.)
            evalMode = True
            savedArticleNum = articleNum
            articleNum = 0

            ENTITIES = TEST_ENTITIES
            CONFIDENCES = TEST_CONFIDENCES
            TFIDF_MATRICES = TEST_TFIDF_MATRICES
            articles, titles, identifiers, downloaded_articles = test_articles, test_titles, test_identifiers, test_downloaded_articles
            # print "##### Evaluation Started ######"

        elif message == "evalEnd":            
            print "------------\nEvaluation Stats: (Precision, Recall, F1):"
            outFile.write("------------\nEvaluation Stats: (Precision, Recall, F1):\n")
            for tag in int2tags:
                prec = CORRECT[tag]/PRED[tag]
                rec = CORRECT[tag]/GOLD[tag]
                f1 = (2*prec*rec)/(prec+rec)
                print tag, prec, rec, f1, "########", CORRECT[tag], PRED[tag], GOLD[tag]
                outFile.write(' '.join([str(tag), str(prec), str(rec), str(f1)])+'\n')
            evalMode = False
            articleNum = savedArticleNum

            ENTITIES = TRAIN_ENTITIES
            CONFIDENCES = TRAIN_CONFIDENCES
            TFIDF_MATRICES = TRAIN_TFIDF_MATRICES
            articles, titles, identifiers, downloaded_articles = train_articles, train_titles, train_identifiers, train_downloaded_articles
            # print "##### Evaluation Ended ######"

            if args.oracle:
                plot_hist(EVALCONF, "conf1")
                plot_hist(EVALCONF2, "conf2")

            #save the extracted entities
            if args.saveEntities:
                pickle.dump([TRAIN_ENTITIES, TRAIN_CONFIDENCES, TRAIN_TFIDF_MATRICES], open("train2.entities", "wb"))
                pickle.dump([TEST_ENTITIES, TEST_CONFIDENCES, TEST_TFIDF_MATRICES], open("test2.entities", "wb"))  
                return 

        else:
            # message is "step"
            action = int(message)

            if evalMode and DEBUG:
                print "State:"
                print newstate[:4]
                print newstate[4:8]
                print newstate[8:]
                print "Entities:", env.prevEntities
                print "Action:", action            
            newstate, reward, terminal = env.step(action)        
            terminal = 'true' if terminal else 'false'

            #remove reward unless terminal
            if args.delayedReward == 'True' and terminal == 'false':
                reward = 0

            if evalMode and DEBUG and reward != 0:
                print "Reward:", reward            
                pdb.set_trace()
        
        if message != "evalStart" and message != "evalEnd":
            #do article eval if terminal
            if evalMode and articleNum < len(articles) and terminal == 'true':
                if args.oracle:
                    env.oracleEvaluate(env.goldEntities, ENTITIES[env.indx], CONFIDENCES[env.indx])                    
                else:
                    env.evaluateArticle(env.bestEntities.values(), env.goldEntities, args.shooterLenientEval, args.shooterLastName, evalOutFile)

            #send message (IMP: only for newGame or step messages)
            outMsg = 'state, reward, terminal = ' + str(newstate) + ',' + str(reward)+','+terminal
            socket.send(outMsg.replace('[', '{').replace(']', '}'))
        else:
            socket.send("done")


if __name__ == '__main__':
    env = None
    newstate, reward, terminal = None, None, None

    argparser = argparse.ArgumentParser(sys.argv[0])
    argparser.add_argument("--port",
        type = int,
        default = 5050,
        help = "port for server")
    argparser.add_argument("--trainFile",
        type = str,
        help = "training File")
    argparser.add_argument("--testFile",
        type = str,
        default = "",
        help = "Testing File")
    argparser.add_argument("--outFile",
        type = str,
        help = "Output File")

    argparser.add_argument("--evalOutFile",
        default = "",
        type = str,
        help = "Output File for predictions")

    argparser.add_argument("--modelFile",
        type = str,
        help = "Model File")

    argparser.add_argument("--shooterLenientEval",
        type = bool,
        default = False,
        help = "Evaluate shooter leniently by counting any match as right")

    argparser.add_argument("--shooterLastName",
        type = bool,
        default = False,
        help = "Evaluate shooter using only last name")

    argparser.add_argument("--oracle",
        type = bool,
        default = False,
        help = "Evaluate using oracle")

    argparser.add_argument("--ignoreDuplicates",
        type = bool,
        default = False,
        help = "Ignore duplicate articles in downloaded ones.")

    argparser.add_argument("--baselineEval",
        type = bool,
        default = False,
        help = "Evaluate baseline performance")

    argparser.add_argument("--thresholdEval",
        type = bool,
        default = False,
        help = "Evaluate baseline performance")

    argparser.add_argument("--shuffleArticles",
        type = bool,
        default = False,
        help = "Shuffle the order of new articles presented to agent")

    argparser.add_argument("--entity",
        type = int,
        default = 4,
        help = "Entity num. 4 means all.")

    #TODO: add code for options 'conf' and 'majority'
    argparser.add_argument("--aggregate",
        type = str,
        default = 'always',
        help = "Options: always, conf, majority")

    argparser.add_argument("--delayedReward",
        type = str,
        default = 'False',
        help = "delay reward to end")

    argparser.add_argument("--trainEntities",
        type = str,
        default = '',
        help = "Pickle file with extracted train entities")

    argparser.add_argument("--testEntities",
        type = str,
        default = '',
        help = "Pickle file with extracted test entities")

    argparser.add_argument("--saveEntities",
        type = bool,
        default = False,
        help = "save extracted entities to file")



    args = argparser.parse_args()

    main(args)

    

        
