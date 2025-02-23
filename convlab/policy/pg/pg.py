# -*- coding: utf-8 -*-
import torch
from torch import optim
import numpy as np
import logging
import os
import json
from convlab.policy.policy import Policy
from convlab.policy.rlmodule import MultiDiscretePolicy
from convlab.util.custom_util import set_seed
from convlab.util.train_util import init_logging_handler
from convlab.policy.vector.vector_binary import VectorBinary
from convlab.util.file_util import cached_path
import zipfile
import sys

root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.append(root_dir)

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class PG(Policy):

    def __init__(self, is_train=False, dataset='Multiwoz', seed=0, vectorizer=None):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json'), 'r') as f:
            cfg = json.load(f)
        self.cfg = cfg
        self.save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg['save_dir'])
        self.save_per_epoch = cfg['save_per_epoch']
        self.update_round = cfg['update_round']
        self.optim_batchsz = cfg['batchsz']
        self.gamma = cfg['gamma']
        self.is_train = is_train
        self.vector = vectorizer
        self.info_dict = {}

        set_seed(seed)

        if self.vector is None:
            logging.info("No vectorizer was set, using default..")
            from convlab.policy.vector.vector_binary import VectorBinary
            self.vector = VectorBinary()

        if dataset == 'Multiwoz':
            self.vector = vectorizer
            self.policy = MultiDiscretePolicy(self.vector.state_dim, cfg['h_dim'], self.vector.da_dim).to(device=DEVICE)

        # self.policy = MultiDiscretePolicy(self.vector.state_dim, cfg['h_dim'], self.vector.da_dim).to(device=DEVICE)
        if is_train:
            self.policy_optim = optim.RMSprop(self.policy.parameters(), lr=cfg['lr'])

    def predict(self, state):
        """
        Predict an system action given state.
        Args:
            state (dict): Dialog state. Please refer to util/state.py
        Returns:
            action : System act, with the form of (act_type, {slot_name_1: value_1, slot_name_2, value_2, ...})
        """
        s, action_mask = self.vector.state_vectorize(state)
        s_vec = torch.Tensor(s)
        mask_vec = torch.Tensor(action_mask)
        a = self.policy.select_action(
            s_vec.to(device=DEVICE), False, action_mask=mask_vec.to(device=DEVICE)).cpu()

        a_counter = 0
        while a.sum() == 0:
            a_counter += 1
            a = self.policy.select_action(
                s_vec.to(device=DEVICE), True, action_mask=mask_vec.to(device=DEVICE)).cpu()
            if a_counter == 5:
                break
        # print('True :')
        # print(a)
        action = self.vector.action_devectorize(a.detach().numpy())
        self.info_dict["action_used"] = action
        # for key in state.keys():
        #     print("Key : {} , Value : {}".format(key,state[key]))
        return action

    def init_session(self):
        """
        Restore after one session
        """
        pass

    def est_return(self, r, mask):
        """
        we save a trajectory in continuous space and it reaches the ending of current trajectory when mask=0.
        :param r: reward, Tensor, [b]
        :param mask: indicates ending for 0 otherwise 1, Tensor, [b]
        :return: V-target(s), Tensor
        """
        batchsz = r.size(0)

        # v_target is worked out by Bellman equation.
        v_target = torch.Tensor(batchsz).to(device=DEVICE)

        prev_v_target = 0
        for t in reversed(range(batchsz)):
            # mask here indicates a end of trajectory
            # this value will be treated as the target value of value network.
            # mask = 0 means the immediate reward is the real V(s) since it's end of trajectory.
            # formula: V(s_t) = r_t + gamma * V(s_t+1)
            v_target[t] = r[t] + self.gamma * prev_v_target * mask[t]

            # update previous
            prev_v_target = v_target[t]

        return v_target

    def update(self, epoch, batchsz, s, a, r, mask, action_mask):

        v_target = self.est_return(r, mask)

        for i in range(self.update_round):

            # 1. shuffle current batch
            perm = torch.randperm(batchsz)
            # shuffle the variable for mutliple optimize
            v_target_shuf, s_shuf, a_shuf, action_mask_shuf = v_target[perm], s[perm], a[perm], action_mask[perm]

            # 2. get mini-batch for optimizing
            optim_chunk_num = int(np.ceil(batchsz / self.optim_batchsz))
            # chunk the optim_batch for total batch
            v_target_shuf, s_shuf, a_shuf, action_mask_shuf = torch.chunk(v_target_shuf, optim_chunk_num), \
                                            torch.chunk(s_shuf, optim_chunk_num), \
                                            torch.chunk(a_shuf, optim_chunk_num), \
                                            torch.chunk(action_mask_shuf, optim_chunk_num)

            # 3. iterate all mini-batch to optimize
            policy_loss = 0.
            for v_target_b, s_b, a_b, action_mask_b in zip(v_target_shuf, s_shuf, a_shuf, action_mask_shuf):
                # print('optim:', batchsz, v_target_b.size(), A_sa_b.size(), s_b.size(), a_b.size(), log_pi_old_sa_b.size())

                # update policy network by clipping
                self.policy_optim.zero_grad()
                # [b, 1]
                log_pi_sa = self.policy.get_log_prob(s_b, a_b, action_mask_b)
                # ratio = exp(log_Pi(a|s) - log_Pi_old(a|s)) = Pi(a|s) / Pi_old(a|s)
                # we use log_pi for stability of numerical operation
                # [b, 1] => [b]
                # this is element-wise comparing.
                # we add negative symbol to convert gradient ascent to gradient descent
                surrogate = - (log_pi_sa * v_target_b).mean()
                policy_loss += surrogate.item()

                # backprop
                surrogate.backward()

                for p in self.policy.parameters():
                    p.grad[p.grad != p.grad] = 0.0
                # gradient clipping, for stability
                torch.nn.utils.clip_grad_norm_(self.policy.parameters(), 10)
                # self.lock.acquire() # retain lock to update weights
                self.policy_optim.step()
                # self.lock.release() # release lock

            policy_loss /= optim_chunk_num
            logging.debug('<<dialog policy pg>> epoch {}, iteration {}, policy, loss {}'.format(epoch, i, policy_loss))

    def save(self, directory, epoch):
        if not os.path.exists(directory):
            os.makedirs(directory)

        torch.save(self.policy.state_dict(), directory + '/' + str(epoch) + '_pg.pol.mdl')

        logging.info('<<dialog policy>> epoch {}: saved network to mdl'.format(epoch))

    def load(self, filename):
        policy_mdl_candidates = [
            filename,
            filename + '.pol.mdl',
            filename + '_pg.pol.mdl',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), filename),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), filename + '.pol.mdl'),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), filename + '_pg.pol.mdl')
        ]
        for policy_mdl in policy_mdl_candidates:
            if os.path.exists(policy_mdl):
                self.policy.load_state_dict(torch.load(policy_mdl, map_location=DEVICE))
                logging.info('<<dialog policy>> loaded checkpoint from file: {}'.format(policy_mdl))
                break

    def load_from_pretrained(self, archive_file, model_file, filename):
        if not os.path.isfile(archive_file):
            if not model_file:
                raise Exception("No model for PG Policy is specified!")
            archive_file = cached_path(model_file)
        model_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'save')
        if not os.path.exists(model_dir):
            os.mkdir(model_dir)
        if not os.path.exists(os.path.join(model_dir, 'best_pg.pol.mdl')):
            archive = zipfile.ZipFile(archive_file, 'r')
            archive.extractall(model_dir)

        policy_mdl = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename + '_pg.pol.mdl')
        if os.path.exists(policy_mdl):
            self.policy.load_state_dict(torch.load(policy_mdl, map_location=DEVICE))
            logging.info('<<dialog policy>> loaded checkpoint from file: {}'.format(policy_mdl))

    @classmethod
    def from_pretrained(cls,
                        archive_file="",
                        model_file="https://huggingface.co/ConvLab/ConvLab-2_models/resolve/main/pg_policy_multiwoz.zip"):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json'), 'r') as f:
            cfg = json.load(f)
        model = cls()
        model.load_from_pretrained(archive_file, model_file, cfg['load'])
        return model