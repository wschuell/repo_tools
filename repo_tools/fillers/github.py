import datetime
import os
import psycopg2
from psycopg2 import extras
import subprocess
import shutil
import copy
import pygit2
import logging
import numpy as np
import glob
import github
import calendar
import time
import sqlite3
import random

import concurrent.futures
from concurrent.futures import ThreadPoolExecutor


from repo_tools import fillers
from repo_tools.fillers import generic
import repo_tools as rp

class GithubFiller(fillers.Filler):
	"""
	class to be inherited from, contains github credentials management
	"""
	def __init__(self,querymin_threshold=50,per_page=100,workers=1,api_keys_file='github_api_keys.txt',fail_on_wait=False,**kwargs):
		self.querymin_threshold = querymin_threshold
		self.per_page = per_page
		self.workers = workers
		self.api_keys_file = api_keys_file
		self.fail_on_wait = fail_on_wait
		fillers.Filler.__init__(self,**kwargs)

	def prepare(self):
		if self.data_folder is None:
			self.data_folder = self.db.data_folder

		self.set_github_requesters()

		if self.db.db_type == 'postgres':
			self.db.cursor.execute(''' INSERT INTO identity_types(name) VALUES('github_login') ON CONFLICT DO NOTHING;''')
		else:
			self.db.cursor.execute(''' INSERT OR IGNORE INTO identity_types(name) VALUES('github_login');''')
		self.db.connection.commit()

	def set_github_requesters(self):
		'''
		Setting github requesters
		api keys file syntax, per line: API#notes
		'''
		api_keys_file = os.path.join(self.data_folder,self.api_keys_file)
		if os.path.exists(api_keys_file):
			with open(api_keys_file,'r') as f:
				api_keys = [l.split('#')[0] for l in f.read().split('\n')]
		else:
			api_keys = []

		try:
			api_keys.append(os.environ['GITHUB_API_KEY'])
		except KeyError:
			pass

		self.github_requesters = [github.Github(per_page=self.per_page)]
		for ak in set(api_keys):
			g = github.Github(ak,per_page=self.per_page)
			try:
				g.get_rate_limit()
			except:
				self.logger.info('API key starting with "{}" and of length {} not valid'.format(ak[:5],len(ak)))
			else:
				self.github_requesters.append(g)

	def get_github_requester(self):
		'''
		Going through requesters respecting threshold of minimum remaining api queries
		'''
		if not hasattr(self,'github_requesters'):
			self.set_github_requesters()
		while True:
			for i,rq in enumerate(self.github_requesters):
				self.logger.debug('Using github requester {}, {} queries remaining'.format(i,rq.get_rate_limit().core.remaining))
				# time.sleep(0.5)
				while rq.get_rate_limit().core.remaining > self.querymin_threshold:
					yield rq
			if any(((rq.get_rate_limit().core.remaining > self.querymin_threshold) for rq in self.github_requesters)):
				continue
			elif self.fail_on_wait:
				raise IOError('All {} API keys are below the min remaining query threshold'.format(len(self.github_requesters)))
			else:
				earliest_reset = min([calendar.timegm(rq.get_rate_limit().core.reset.timetuple()) for rq in self.github_requesters])
				time_to_reset =  earliest_reset - calendar.timegm(time.gmtime())
				self.logger.info('Waiting for reset of at least one github requester, sleeping {} seconds'.format(time_to_reset+1))
				time.sleep(time_to_reset+1)


class StarsFiller(GithubFiller):
	"""
	Fills in star information
	"""
	def __init__(self,force=False,retry=False,repo_list=None,**kwargs):
		self.force = force
		self.retry = retry
		self.repo_list = repo_list
		GithubFiller.__init__(self,**kwargs)


	def apply(self):
		self.fill_stars(force=self.force,retry=self.retry,repo_list=self.repo_list,workers=self.workers)
		self.db.connection.commit()

	def fill_stars(self,force=False,retry=False,repo_list=None,workers=1,in_thread=False):
		'''
		Filling stars (only from github for the moment)
		force can be True, or an integer representing an acceptable delay in seconds for age of last update

		Checking if an entry exists in table_updates with repo_id and table_name stars

		repo syntax: (source,owner,name,repo_id,star_update)
		'''

		if repo_list is None:
			#build repo list
			repo_list = []
			for r in self.db.get_repo_list(option='starinfo'):
				# created_at = self.db.get_last_star(source=r['source'],repo=r['name'],owner=r['owner'])['created_at']
				# created_at = self.db.get_last_star(source=r[0],repo=r[2],owner=r[1])['created_at']
				# created_at = self.db.get_last_star(source=r[0],repo=r[2],owner=r[1])['created_at']
				source,owner,repo_name,repo_id,created_at,success = r[:6]

				if isinstance(created_at,str):
					created_at = datetime.datetime.strptime(created_at,'%Y-%m-%d %H:%M:%S')


				if (force==True) or (created_at is None) or ((not isinstance(force,bool)) and time.time()-created_at.timestamp()>force) or (retry and not success):
					# repo_list.append('{}/{}'.format(r['name'],r['owner']))
					# repo_list.append('{}/{}'.format(r[2],r[3]))
					repo_list.append(r)

		if workers == 1:
			requester_gen = self.get_github_requester()
			if in_thread:
				db = self.db.copy()
			else:
				db = self.db
			new_repo = True
			while len(repo_list):
				current_repo = repo_list[0]
				# owner,repo_name = current_repo.split('/')
				# source = 'GitHub'
				# repo_id = db.get_repo_id(owner=owner,source=source,name=repo_name)
				source,owner,repo_name,repo_id = current_repo[:4]
				if new_repo:
					new_repo = False
					self.logger.info('Filling stars for repo {}/{}'.format(owner,repo_name))
				requester = next(requester_gen)
				try:
					repo_apiobj = requester.get_repo('{}/{}'.format(owner,repo_name))
				except github.GithubException:
					self.logger.info('No such repository: {}/{}'.format(owner,repo_name))
					db.insert_update(repo_id=repo_id,table='stars',success=False)
					repo_list.pop(0)
					new_repo = True
				else:
					while requester.get_rate_limit().core.remaining > self.querymin_threshold:
						nb_stars = db.count_stars(source=source,repo=repo_name,owner=owner)
						# sg_list = list(repo_apiobj.get_stargazers_with_dates()[nb_stars:nb_stars+per_page])
						sg_list = list(repo_apiobj.get_stargazers_with_dates().get_page(int(nb_stars/self.per_page)))

						if nb_stars < self.per_page*(int(nb_stars/self.per_page))+len(sg_list):
							# if in_thread:
							if db.db_type == 'sqlite' and in_thread:
								time.sleep(1+random.random()) # to avoid database locked issues, and smooth a bit concurrency
							# db.insert_stars(stars_list=[{'repo_id':repo_id,'source':source,'repo':repo_name,'owner':owner,'starred_at':sg.starred_at,'login':sg.user.login} for sg in sg_list],commit=False)
							self.insert_stars(db=db,stars_list=[{'repo_id':repo_id,'source':source,'repo':repo_name,'owner':owner,'starred_at':sg.starred_at,'login':sg.user.login} for sg in sg_list],commit=False)
						else:
							self.logger.info('Filled stars for repo {}/{}: {}'.format(owner,repo_name,nb_stars))
							db.insert_update(repo_id=repo_id,table='stars',success=True)
							db.connection.commit()
							repo_list.pop(0)
							new_repo = True
							break
			if in_thread:
				db.cursor.close()
				db.connection.close()

		else:
			with ThreadPoolExecutor(max_workers=workers) as executor:
				futures = []
				for repo in repo_list:
					futures.append(executor.submit(self.fill_stars,repo_list=[repo],workers=1,in_thread=True))
				# for future in concurrent.futures.as_completed(futures):
				# 	pass
				for future in futures:
					future.result()


	def insert_stars(self,stars_list,commit=True,db=None):
		'''
		Inserts starring events.
		commit defines the behavior at the end, commit of the transaction or not. Committing externally allows to do it only when all stars for a repo have been added
		'''
		if db is None:
			db = self.db
		if db.db_type == 'postgres':
			extras.execute_batch(db.cursor,'''
				INSERT INTO stars(starred_at,login,repo_id,identity_type_id,identity_id)
				VALUES(%s,
						%s,
						%s,
						(SELECT id FROM identity_types WHERE name='github_login'),
						(SELECT id FROM identities WHERE identity=%s AND identity_type_id=(SELECT id FROM identity_types WHERE name='github_login'))
					)
				ON CONFLICT DO NOTHING
				;''',((s['starred_at'],s['login'],s['repo_id'],s['login']) for s in stars_list))
		else:
			db.cursor.executemany('''
					INSERT OR IGNORE INTO stars(starred_at,login,repo_id,identity_type_id,identity_id)
					VALUES(?,
							?,
							?,
							(SELECT id FROM identity_types WHERE name='github_login'),
							(SELECT id FROM identities WHERE identity=? AND identity_type_id=(SELECT id FROM identity_types WHERE name='github_login'))
						);''',((s['starred_at'],s['login'],s['repo_id'],s['login']) for s in stars_list))

		if commit:
			db.connection.commit()


class GHLoginsFiller(GithubFiller):
	"""
	Fills in github login information
	"""
	def __init__(self,force=False,info_list=None,**kwargs):
		self.force = force
		self.info_list = info_list
		GithubFiller.__init__(self,**kwargs)



	def apply(self):
		self.fill_gh_logins(info_list=self.info_list)
		self.db.connection.commit()

	def prepare(self):
		GithubFiller.prepare(self)
		if self.info_list is None:

			if self.force:
				if self.db.db_type == 'postgres':
					self.db.cursor.execute('''
						SELECT i.id,c.repo_id,r.owner,r.name,c.sha
						FROM identities i
						JOIN LATERAL (SELECT cc.sha,cc.repo_id FROM commits cc
							WHERE cc.author_id=i.id ORDER BY cc.created_at DESC LIMIT 1) AS c
						ON (SELECT i2.id FROM identities i2 WHERE i2.user_id=i.user_id AND i2.identity_type_id=(SELECT it.id FROM identity_types WHERE name='github_login')) IS NULL
						INNER JOIN repositories r
						ON r.id=c.repo_id
						;''')
				else:
					self.db.cursor.execute('''
						SELECT u.id,c.repo_id,r.owner,r.name,c.sha
						FROM identities i
						JOIN commits c
							ON (SELECT i2.id FROM identities i2 WHERE i2.user_id=i.user_id AND i2.identity_type_id=(SELECT it.id FROM identity_types WHERE name='github_login')) IS NULL AND
							c.id IN (SELECT cc.id FROM commits cc
								WHERE cc.author_id=i.id ORDER BY cc.created_at DESC LIMIT 1)
						INNER JOIN repositories r
						ON r.id=c.repo_id
						;''')


			else:
				if self.db.db_type == 'postgres':
					self.db.cursor.execute('''
						SELECT i.id,c.repo_id,r.owner,r.name,c.sha
						FROM (
							SELECT ii.id FROM
						 		(SELECT iii.id FROM identities iii
								WHERE (SELECT iiii.id FROM identities iiii
									INNER JOIN identity_types iiiit
									ON iiii.user_id=iii.user_id AND iiiit.id=iiii.identity_type_id AND iiiit.name='github_login') IS NULL) AS ii
								LEFT JOIN table_updates tu
								ON tu.identity_id=ii.id AND tu.table_name='login'
								GROUP BY ii.id,tu.identity_id
								HAVING tu.identity_id IS NULL
							) AS i
						JOIN LATERAL (SELECT cc.sha,cc.repo_id FROM commits cc
							WHERE cc.author_id=i.id ORDER BY cc.created_at DESC LIMIT 1) AS c
						ON true
						INNER JOIN repositories r
						ON r.id=c.repo_id
						;''')
				else:
					self.db.cursor.execute('''
						SELECT i.id,c.repo_id,r.owner,r.name,c.sha
						FROM (
							SELECT ii.id FROM
						 		(SELECT iii.id FROM identities iii
								WHERE (SELECT iiii.id FROM identities iiii
									INNER JOIN identity_types iiiit
									ON iiii.user_id=iii.user_id AND iiiit.id=iiii.identity_type_id AND iiiit.name='github_login') IS NULL) AS ii
								LEFT JOIN table_updates tu
								ON tu.identity_id=ii.id AND tu.table_name='login'
								GROUP BY ii.id,tu.identity_id
								HAVING tu.identity_id IS NULL
							) AS i
						JOIN commits c
							ON
							c.id IN (SELECT cc.id FROM commits cc
								WHERE cc.author_id=i.id ORDER BY cc.created_at DESC LIMIT 1)
						INNER JOIN repositories r
						ON r.id=c.repo_id
						;''')

			self.info_list = list(self.db.cursor.fetchall())

	def fill_gh_logins(self,info_list=None,workers=1,in_thread=False):
		'''
		Associating emails to github logins using GitHub API
		force: retry emails that were previously not retrievable
		Otherwise trying all emails which have no login yet and never failed before
		'''

		if info_list is None:
			info_list = self.info_list

		if workers == 1:
			requester_gen = self.get_github_requester()
			if in_thread:
				db = self.db.copy()
			else:
				db = self.db
			for infos in info_list:
				identity_id,repo_id,repo_owner,repo_name,commit_sha = infos
				self.logger.info('Filling gh login for user id {}'.format(identity_id))
				requester = next(requester_gen)
				try:
					repo_apiobj = requester.get_repo('{}/{}'.format(repo_owner,repo_name))
					try:
						commit_apiobj = repo_apiobj.get_commit(commit_sha)
					except github.GithubException:
						self.logger.info('No such commit: {}/{}/{}'.format(repo_owner,repo_name,commit_sha))
						commit_apiobj = None
				except github.GithubException:
					self.logger.info('No such repository: {}/{}'.format(repo_owner,repo_name))
					# db.insert_update(identity_id=identity_id,table='stars',success=False)
				else:
					if commit_apiobj is None:
						pass
					else:
						if commit_apiobj.author is None:
							login = None
							self.logger.info('No login available for user id {}'.format(identity_id))
						else:
							try:
								login = commit_apiobj.author.login
								self.logger.info('Found login {} for user id {}'.format(login,identity_id))
							except github.GithubException:
								self.logger.info('No login available for user id {}, uncompletable object error'.format(identity_id))
								login = None
						self.set_gh_login(db=db,identity_id=identity_id,login=login,reason='Email/login match through github API for commit {}'.format(commit_sha))
			if in_thread:
				db.cursor.close()
				db.connection.close()
		else:
			with ThreadPoolExecutor(max_workers=workers) as executor:
				futures = []
				for infos in info_list:
					futures.append(executor.submit(self.fill_gh_logins,info_list=[infos],workers=1,in_thread=True))
				# for future in concurrent.futures.as_completed(futures):
				# 	pass
				for future in futures:
					future.result()

	def set_gh_login(self,identity_id,login,autocommit=True,db=None,reason=None):
		'''
		Sets a login for a given user (id refers to a unique email, which can refer to several logins)
		'''
		if db is None:
			db = self.db
		if db.db_type == 'postgres':
			if login is not None:
				db.cursor.execute(''' INSERT INTO users(creation_identity_type_id,creation_identity) VALUES(
											(SELECT id FROM identity_types WHERE name='github_login'),
											%s
											) ON CONFLICT DO NOTHING;''',(login,))

				db.cursor.execute(''' INSERT INTO identities(identity_type_id,user_id,identity)
												VALUES((SELECT id FROM identity_types WHERE name='github_login'),
														(SELECT id FROM users
														WHERE creation_identity_type_id=(SELECT id FROM identity_types WHERE name='github_login')
															AND creation_identity=%s),
														%s)
												ON CONFLICT DO NOTHING;''',(login,login,))

				db.cursor.execute('''SELECT id FROM identities
											WHERE identity_type_id=(SELECT id FROM identity_types WHERE name='github_login')
											AND identity=%s;''',(login,))
				identity2 = db.cursor.fetchone()[0]
				db.merge_identities(identity1=identity_id,identity2=identity2,autocommit=False,reason=reason)
			db.cursor.execute('''INSERT INTO table_updates(identity_id,table_name,success) VALUES(%s,'login',%s);''',(identity_id,(login is not None)))
		else:
			if login is not None:


				db.cursor.execute(''' INSERT OR IGNORE INTO users(creation_identity_type_id,creation_identity) VALUES(
											(SELECT id FROM identity_types WHERE name='github_login'),
											?
											);''',(login,))

				db.cursor.execute(''' INSERT OR IGNORE INTO identities(identity_type_id,user_id,identity)
												VALUES((SELECT id FROM identity_types WHERE name='github_login'),
														(SELECT id FROM users
														WHERE creation_identity_type_id=(SELECT id FROM identity_types WHERE name='github_login')
															AND creation_identity=?),
														?);''',(login,login,))

				db.cursor.execute('''SELECT id FROM identities
											WHERE identity_type_id=(SELECT id FROM identity_types WHERE name='github_login')
											AND identity=?;''',(login,))
				identity2 = db.cursor.fetchone()[0]


				db.merge_identities(identity1=identity_id,identity2=identity2,autocommit=False,reason=reason)

			db.cursor.execute('''INSERT INTO table_updates(identity_id,table_name,success) VALUES(?,'login',?);''',(identity_id,(login is not None)))
		if autocommit:
			db.connection.commit()


class ForksFiller(GithubFiller):
	"""
	Fills in forks info for github repositories
	"""
	def __init__(self,force=False,repo_list=None,**kwargs):
		self.force = force
		self.repo_list = repo_list
		GithubFiller.__init__(self,**kwargs)



	def apply(self):
		self.fill_forks(repo_list=self.repo_list,force=self.force)
		self.fill_fork_ranks()
		self.db.connection.commit()


	def fill_forks(self,repo_list=None,force=False,workers=1,in_thread=False):
		'''
		Retrieving fork information from github.
		force: retry repos that were previously not retrievable
		Otherwise trying all emails which have no login yet and never failed before
		'''

		if repo_list is None:
			#build repo list
			repo_list = []
			for r in self.db.get_repo_list(option='forkinfo'):
				# created_at = self.db.get_last_star(source=r['source'],repo=r['name'],owner=r['owner'])['created_at']
				# created_at = self.db.get_last_star(source=r[0],repo=r[2],owner=r[1])['created_at']
				# created_at = self.db.get_last_star(source=r[0],repo=r[2],owner=r[1])['created_at']
				source,owner,repo_name,repo_id,created_at,success = r[:6]

				if isinstance(created_at,str):
					created_at = datetime.datetime.strptime(created_at,'%Y-%m-%d %H:%M:%S')


				if (force==True) or (created_at is None) or ((not isinstance(force,bool)) and time.time()-created_at.timestamp()>force) or (retry and not success):
					# repo_list.append('{}/{}'.format(r['name'],r['owner']))
					# repo_list.append('{}/{}'.format(r[2],r[3]))
					repo_list.append(r)


		if workers == 1:
			requester_gen = self.get_github_requester()
			if in_thread:
				db = self.db.copy()
			else:
				db = self.db
			new_repo = True
			while len(repo_list):
				current_repo = repo_list[0]
				# owner,repo_name = current_repo.split('/')
				# source = 'GitHub'
				# repo_id = db.get_repo_id(owner=owner,source=source,name=repo_name)
				source,owner,repo_name,repo_id = current_repo[:4]
				if new_repo:
					new_repo = False
					self.logger.info('Filling forks for repo {}/{}'.format(owner,repo_name))
				requester = next(requester_gen)
				try:
					repo_apiobj = requester.get_repo('{}/{}'.format(owner,repo_name))
				except github.GithubException:
					self.logger.info('No such repository: {}/{}'.format(owner,repo_name))
					db.insert_update(repo_id=repo_id,table='forks',success=False)
					repo_list.pop(0)
					new_repo = True
				else:
					while requester.get_rate_limit().core.remaining > self.querymin_threshold:
						nb_forks = db.count_forks(source=source,repo=repo_name,owner=owner)
						# sg_list = list(repo_apiobj.get_stargazers_with_dates()[nb_stars:nb_stars+per_page])
						sg_list = list(repo_apiobj.get_forks().get_page(int(nb_forks/self.per_page)))
						forks_list=[{'repo_id':repo_id,'source':source,'repo':repo_name,'owner':owner,'repo_fullname':sg.full_name,'created_at':sg.created_at} for sg in sg_list]

						if nb_forks < self.per_page*(int(nb_forks/self.per_page))+len(sg_list):
							# if in_thread:
							if db.db_type == 'sqlite' and in_thread:
								time.sleep(1+random.random()) # to avoid database locked issues, and smooth a bit concurrency
							if self.db.db_type == 'postgres':
								extras.execute_batch(db.cursor,'''
									INSERT INTO forks(forking_repo_id,forked_repo_id,forking_repo_url,forked_at)
									VALUES((SELECT r.id FROM repositories r
												INNER JOIN sources s
												ON s.name=%s AND s.id=r.source AND CONCAT(r.owner,'/',r.name)=%s),
											%s,
											%s,
											%s)
									ON CONFLICT DO NOTHING
									;''',((s['source'],s['repo_fullname'],s['repo_id'],'github.com/'+s['repo_fullname'],s['created_at']) for s in forks_list))
							else:
								db.cursor.executemany('''
									INSERT OR IGNORE INTO forks(forking_repo_id,forked_repo_id,forking_repo_url,forked_at)
									VALUES((SELECT r.id FROM repositories r
												INNER JOIN sources s
												ON s.name=? AND s.id=r.source AND r.owner || '/' || r.name =?),
											?,
											?,
											?)
									;''',((s['source'],s['repo_fullname'],s['repo_id'],'github.com/'+s['repo_fullname'],s['created_at']) for s in forks_list))

							#db.insert_forks(,commit=False)
						else:
							self.logger.info('Filled forks for repo {}/{}: {}'.format(owner,repo_name,nb_forks))
							db.insert_update(repo_id=repo_id,table='forks',success=True)
							db.connection.commit()
							repo_list.pop(0)
							new_repo = True
							break

			if in_thread:
				db.cursor.close()
				db.connection.close()
		else:
			with ThreadPoolExecutor(max_workers=workers) as executor:
				futures = []
				for infos in info_list:
					futures.append(executor.submit(self.fill_gh_logins,info_list=[infos],workers=1,in_thread=True))
				# for future in concurrent.futures.as_completed(futures):
				# 	pass
				for future in futures:
					future.result()

	def fill_fork_ranks(self,step=1):
		self.logger.info('Filling fork ranks, step {}'.format(step))
		if self.db.db_type == 'postgres':
			self.db.cursor.execute('''
				INSERT INTO forks(forking_repo_id,forking_repo_url,forked_repo_id,forked_at,fork_rank)
				SELECT f2.forking_repo_id,f2.forking_repo_url,f1.forked_repo_id,f2.forked_at,f2.fork_rank+1
						FROM forks f1
						INNER JOIN forks f2
						ON f1.forked_repo_id=f2.forking_repo_id
				ON CONFLICT DO NOTHING
				;
				''')
			rowcount = self.db.cursor.rowcount
		else:
			self.db.cursor.execute('''
				INSERT OR IGNORE INTO forks(forking_repo_id,forking_repo_url,forked_repo_id,forked_at,fork_rank)
					SELECT f2.forking_repo_id,f2.forking_repo_url,f1.forked_repo_id,f2.forked_at,f2.fork_rank+1
						FROM forks f1
						INNER JOIN forks f2
						ON f2.forked_repo_id=f1.forking_repo_id

				;
				''')
			rowcount = self.db.cursor.rowcount
		if rowcount > 0:
			self.logger.info('Filled {} missing indirect fork relations'.format(rowcount))
			self.fill_fork_ranks(step=step+1)

class FollowersFiller(GithubFiller):
	"""
	Fills in follower information
	"""

	def __init__(self,force=False,retry=False,login_list=None,**kwargs):
		self.force = force
		self.retry = retry
		self.login_list = login_list
		GithubFiller.__init__(self,**kwargs)


	def apply(self):
		self.fill_followers(retry=self.retry,login_list=self.login_list,workers=self.workers)
		self.db.connection.commit()


	# def fill_followers(self,login_list=None,workers=1,in_thread=False,time_delay=24*3600):
	# 	'''
	# 	Getting followers for github logins. Avoiding by default logins which already have a value from less than time_delay seconds ago.
	# 	'''

	# 	option = 'logins'

	# 	if login_list is None:
	# 		login_list = self.db.get_user_list(option=option,time_delay=time_delay)

	# 	if workers == 1:
	# 		requester_gen = self.get_github_requester()
	# 		if in_thread:
	# 			db = self.db.copy()
	# 		else:
	# 			db = self.db
	# 		for login in login_list:
	# 			self.logger.info('Filling followers for login {}'.format(login))
	# 			requester = next(requester_gen)
	# 			try:
	# 				user_apiobj = requester.get_user('{}'.format(login))
	# 			except github.GithubException:
	# 				self.logger.info('No such user: {}'.format(login))
	# 			else:
	# 				try:
	# 					followers = user_apiobj.followers
	# 					self.logger.info('Login {} has {} followers'.format(login,followers))
	# 				except github.GithubException:
	# 					self.logger.info('No followers info available for login {}, uncompletable object error'.format(login))
	# 					followers = None

	# 				db.fill_followers(followers_info_list=[(login,followers)])

	# 		if in_thread:
	# 			db.connection.close()
	# 			del db
	# 	else:
	# 		with ThreadPoolExecutor(max_workers=workers) as executor:
	# 			futures = []
	# 			for login in login_list:
	# 				futures.append(executor.submit(self.fill_followers,login_list=[login],workers=1,in_thread=True))
	# 			# for future in concurrent.futures.as_completed(futures):
	# 			# 	pass
	# 			for future in futures:
	# 				future.result()


	def prepare(self):
		GithubFiller.prepare(self)
		if self.login_list is None:
			#build login list
			if self.force:
				self.db.cursor.execute('''
					SELECT i.id,i.identity,i.identity_type_id FROM identities i
					INNER JOIN identity_types it
					ON it.id=i.identity_type_id AND it.name='github_login';
					''')
			else:
				self.db.cursor.execute('''
					SELECT i.id,i.identity,i.identity_type_id FROM identities i
					INNER JOIN identity_types it
					ON it.id=i.identity_type_id AND it.name='github_login'
					EXCEPT
					SELECT i.id,i.identity,i.identity_type_id FROM identities i
					INNER JOIN identity_types it
					ON it.id=i.identity_type_id AND it.name='github_login'
					INNER JOIN table_updates tu
					ON tu.success AND tu.identity_id=i.id AND tu.table_name='followers';
					''')
			self.login_list = list(self.db.cursor.fetchall())

	def fill_followers(self,retry=False,login_list=None,workers=1,in_thread=False):
		'''
		Filling followers
		Checking if an entry exists in table_updates with login_id and table_name followers

		login syntax: (source,owner,name,repo_id,follower_update)
		'''

		if workers == 1:
			requester_gen = self.get_github_requester()
			if in_thread:
				db = self.db.copy()
			else:
				db = self.db
			new_login = True
			while len(login_list):
				login_id,login,identity_type_id = login_list[0]

				if new_login:
					new_login = False
					self.logger.info('Filling followers for login {}'.format(login))
				requester = next(requester_gen)
				try:
					login_apiobj = requester.get_user('{}'.format(login))
				except github.GithubException:
					self.logger.info('No such login: {}'.format(login))
					db.insert_update(identity_id=login_id,table='followers',success=False)
					login_list.pop(0)
					new_login = True
				else:
					while requester.get_rate_limit().core.remaining > self.querymin_threshold:
						nb_followers = db.count_followers(login_id=login_id)
						sg_list = list(login_apiobj.get_followers().get_page(int(nb_followers/self.per_page)))

						if nb_followers < self.per_page*(int(nb_followers/self.per_page))+len(sg_list):
							if db.db_type == 'sqlite' and in_thread:
								time.sleep(1+random.random()) # to avoid database locked issues, and smooth a bit concurrency
							self.insert_followers(db=db,followers_list=[{'login_id':login_id,'identity_type_id':identity_type_id,'login':login,'follower_login':sg.login} for sg in sg_list],commit=False)
						else:
							self.logger.info('Filled followers for login {}: {}'.format(login,nb_followers))
							db.insert_update(identity_id=login_id,table='followers',success=True)
							db.connection.commit()
							login_list.pop(0)
							new_login = True
							break
			if in_thread:
				db.cursor.close()
				db.connection.close()

		else:
			with ThreadPoolExecutor(max_workers=workers) as executor:
				futures = []
				for login in login_list:
					futures.append(executor.submit(self.fill_followers,login_list=[login],workers=1,in_thread=True))
				# for future in concurrent.futures.as_completed(futures):
				# 	pass
				for future in futures:
					future.result()


	def insert_followers(self,followers_list,commit=True,db=None):
		'''
		Inserts followers. Syntax [{'identity_type_id':<>,'follower_id':<>,'follower_login':<>,'login_id':<>,'login':<>}]
		commit defines the behavior at the end, commit of the transaction or not. Committing externally allows to do it only when all followers for a login have been added
		'''
		if db is None:
			db = self.db
		if db.db_type == 'postgres':
			extras.execute_batch(db.cursor,'''
				INSERT INTO followers(follower_identity_type_id,follower_login,follower_id,followee_id)
				VALUES(%s,
						%s,
						(SELECT id FROM identities WHERE identity=%s AND identity_type_id=%s),
						%s
					)
				ON CONFLICT DO NOTHING
				;''',((f['identity_type_id'],f['follower_login'],f['follower_login'],f['identity_type_id'],f['login_id'],) for f in followers_list))
		else:
			db.cursor.executemany('''
				INSERT OR IGNORE INTO followers(follower_identity_type_id,follower_login,follower_id,followee_id)
				VALUES(?,
						?,
						(SELECT id FROM identities WHERE identity=? AND identity_type_id=?),
						?
					)
				;''',((f['identity_type_id'],f['follower_login'],f['follower_login'],f['identity_type_id'],f['login_id'],) for f in followers_list))
		if commit:
			db.connection.commit()

