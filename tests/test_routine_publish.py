from copy import deepcopy
import pytest
from app.hevy.routine_publish import PublicationBlocked, publish_initial, publication_summary
from app.training_plan import WeeklyPlan, save_plan_proposal
from tests.test_app import signed_in
from tests.test_training_plan import example_plan

class FakeAPI:
    def __init__(self):
        self.folders=[]; self.routines={}; self.posts=[]; self.lose_response=False
    def all(self,path,key):
        return deepcopy(self.folders if key=='routine_folders' else list(self.routines.values()))
    def request(self,method,path,**kwargs):
        if method=='POST':
            self.posts.append(path)
            if path=='/v1/routine_folders':
                row={'id':123,**kwargs['json']['routine_folder']};self.folders.append(row)
                return {'routine_folder':row}
            row={'id':'new-'+str(len(self.routines)),**kwargs['json']['routine']}
            self.routines[row['id']]=deepcopy(row)
            if self.lose_response:
                self.lose_response=False
                raise PublicationBlocked('Response lost after creation')
            return {'routine':row}
        if path.rsplit('/',1)[-1] not in self.routines: raise PublicationBlocked('Missing routine')
        return deepcopy(self.routines[path.rsplit('/',1)[-1]])

@pytest.fixture
def prepared(tmp_path,monkeypatch):
    monkeypatch.setenv('DATABASE_PATH',str(tmp_path/'locks'/'data.db'))
    _,app=signed_in(tmp_path)
    saved=save_plan_proposal(app.state.db,WeeklyPlan.model_validate(example_plan()),'2026-11-01T00:00:00Z')
    return app.state.db,saved,[{'title':'Machine press','id':'verified-template','type':'weight_reps'}],FakeAPI()

def test_exact_approval_precedes_writes_and_reruns_never_duplicate(prepared):
    db,saved,catalog,api=prepared
    with pytest.raises(PublicationBlocked):
        publish_initial(db,api,catalog,approved_fingerprint='wrong',approval_note='yes')
    assert api.posts==[] and db.publication_events()==[]
    assert publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='Human explicitly requested this plan')['complete']
    assert api.posts==['/v1/routine_folders','/v1/routines']
    assert db.publication_events()[0]['event']=='approved'
    assert publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='same')['complete']
    assert len(api.posts)==2
    assert db.latest_coach_reply()['status']=='plan_draft'
    for index in range(25):db.save_coach_reply(str(index),'answered','question',{'message':'answer'})
    assert publication_summary(db,saved)['complete']

def test_uncertain_post_reconciles_existing_id_instead_of_duplicate(prepared):
    db,saved,catalog,api=prepared;api.lose_response=True
    with pytest.raises(PublicationBlocked):
        publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='yes')
    assert len(api.routines)==1
    assert publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='yes')['complete']
    assert api.posts.count('/v1/routines')==1

def test_missing_binding_and_new_plan_do_not_create_replacements(prepared):
    db,saved,catalog,api=prepared
    publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='yes')
    api.routines.clear()
    with pytest.raises(PublicationBlocked):
        publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='yes')
    assert len(api.posts)==2

    changed=example_plan();changed['days'][0]['name']='New name'
    newer=save_plan_proposal(db,WeeklyPlan.model_validate(changed),'2026-11-02T00:00:00Z')
    with pytest.raises(PublicationBlocked,match='another plan version'):
        publish_initial(db,api,catalog,approved_fingerprint=newer['plan_fingerprint'],approval_note='yes')
    assert len(api.posts)==2


def test_unknown_template_blocks_before_remote_writes(prepared):
    db,saved,catalog,api=prepared
    catalog[0]['type']='reps_only'
    with pytest.raises(PublicationBlocked,match='type does not match'):
        publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='yes')
    assert api.posts==[] and db.publication_events()==[]


def test_missing_published_folder_is_not_recreated(prepared):
    db,saved,catalog,api=prepared
    publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='yes')
    api.folders.clear()
    with pytest.raises(PublicationBlocked,match='bound folder is missing'):
        publish_initial(db,api,catalog,approved_fingerprint=saved['plan_fingerprint'],approval_note='yes')
    assert len(api.posts)==2
